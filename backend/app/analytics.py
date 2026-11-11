from datetime import date, timedelta
from contextlib import closing
import json
import sqlite3

import pandas as pd

from .audit import record
from .config import Settings
from .models import Filters, WEEKDAYS
from .policies import counter_rule, service_rule


def rows(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient='records', date_format='iso'))


def number(value, digits=2):
    return None if pd.isna(value) else round(float(value), digits)


def queue_statistics(frame: pd.DataFrame) -> dict:
    valid = frame['immigration_wait_min'].dropna()
    return {
        'sampled_passengers': len(frame), 'measured_passengers': len(valid),
        'average_wait': number(valid.mean()),
        'pct_over_30': number(valid.gt(30).mean() * 100) if len(valid) else None,
        'estimated_affected_passengers': int(valid.gt(30).sum()) * 25,
    }


class Analytics:
    def __init__(self, settings: Settings):
        self.settings = settings

    def query(self, sql: str, params=()) -> pd.DataFrame:
        with closing(sqlite3.connect(self.settings.database.as_uri() + '?mode=ro', uri=True)) as con:
            frame = pd.read_sql_query(sql, con, params=params)
        record(self.settings.logs, 'database_select', {'sql': sql, 'params': list(params)}, rows(frame))
        return frame

    def metadata(self):
        bounds = self.query('SELECT MIN(journey_date) AS date_from, MAX(journey_date) AS date_to FROM passenger_journeys')
        return {
            **bounds.iloc[0].to_dict(), 'terminals': ['T1', 'T2', 'ALL'],
            'weekdays': ['All', *WEEKDAYS], 'sample_rate': 0.04,
            'sample_multiplier': 25, 'fictional': True,
        }

    def dashboard(self, filters: Filters) -> dict:
        f = filters
        terminal_sql = '' if f.terminal == 'ALL' else ' AND terminal_id = ?'
        terminal_params = () if f.terminal == 'ALL' else (f.terminal,)
        # Two preceding days retain consecutive-hour context across midnight.
        sample = self.query(
            'SELECT journey_date, weekday, immigration_hour, terminal_id, immigration_wait_min, used_smart_gate, missed_flight '
            'FROM passenger_journeys WHERE direction = ? AND journey_date BETWEEN ? AND ?' + terminal_sql,
            ('Departure', (f.date_from - timedelta(days=2)).isoformat(), f.date_to.isoformat(), *terminal_params),
        )
        selection = sample[
            sample.journey_date.ge(f.date_from.isoformat()) &
            sample.immigration_hour.between(f.hour_from, f.hour_to)
        ].copy()
        if f.weekday != 'All':
            selection = selection[selection.weekday.eq(f.weekday)].copy()

        roster_sql = '' if f.terminal == 'ALL' else ' AND checkpoint_id = ?'
        roster_params = () if f.terminal == 'ALL' else (f.terminal + '-IMM-DEP',)
        roster = self.query(
            "SELECT roster_date, hour, checkpoint_id, counters_open FROM staffing_roster "
            "WHERE roster_date BETWEEN ? AND ? AND checkpoint_id IN ('T1-IMM-DEP', 'T2-IMM-DEP')" + roster_sql,
            (f.date_from.isoformat(), f.date_to.isoformat(), *roster_params),
        )
        meta = self.metadata()
        first = max(f.date_from, date.fromisoformat(meta['date_from']))
        last = min(f.date_to, date.fromisoformat(meta['date_to']))
        terminals = ['T1', 'T2'] if f.terminal == 'ALL' else [f.terminal]
        expected = []
        for timestamp in pd.date_range(first, last):
            day = timestamp.date()
            if f.weekday != 'All' and timestamp.day_name() != f.weekday:
                continue
            for terminal in terminals:
                for hour in range(f.hour_from, f.hour_to + 1):
                    rule = counter_rule(day, hour, terminal)
                    expected.append({
                        'roster_date': day.isoformat(), 'hour': hour, 'terminal_id': terminal,
                        'checkpoint_id': terminal + '-IMM-DEP', 'weekday': timestamp.day_name(),
                        'month': day.strftime('%Y-%m'), 'minimum': rule['minimum'], 'policy': rule['source'],
                    })
        columns = ['roster_date', 'hour', 'terminal_id', 'checkpoint_id', 'weekday', 'month', 'minimum', 'policy']
        coverage = pd.DataFrame(expected, columns=columns).merge(
            roster, on=['roster_date', 'hour', 'checkpoint_id'], how='left', validate='one_to_one',
        )
        coverage['missing'] = coverage.counters_open.isna()
        coverage['shortfall'] = coverage.counters_open.lt(coverage.minimum) & ~coverage['missing']
        observed_hours = int((~coverage['missing']).sum())
        missing_hours = int(coverage['missing'].sum())
        exclusions = [
            f'{missing_hours} missing checkpoint-hours excluded from counter averages and shortfall assessment; not treated as zero.',
            'Passenger counts estimated from a 4% sample; averages and percentages are not scaled.',
            'Selected-date summaries are descriptive. Formal service assessment uses monthly terminal/weekday/hour groups.',
            'Partial-month results are provisional. No minimum waiting-time sample size is specified in the supplied policy.',
            'Incident overlaps do not establish causation or prove an operational response occurred.',
        ]
        if not selection.empty:
            selection['month'] = selection.journey_date.str[:7]
        monthly = []
        if not selection.empty:
            for (month, terminal, weekday, hour), group in selection.groupby(['month', 'terminal_id', 'weekday', 'immigration_hour']):
                stats = queue_statistics(group)
                day = date.fromisoformat(month + '-01')
                rule = service_rule(day, int(hour))
                month_end = (pd.Timestamp(day) + pd.offsets.MonthEnd(0)).date()
                partial = f.date_from > day or f.date_to < month_end
                avg = stats['average_wait']
                tail = stats['pct_over_30']
                # Compare full precision, never rounded display values at a boundary.
                values = group.immigration_wait_min.dropna()
                breach = bool(len(values) and (values.mean() > rule['average_limit'] or (rule['peak'] and values.gt(30).mean() * 100 > rule['tail_limit'])))
                status = 'Not assessable' if avg is None else ('Breach' if breach else 'Within target')
                if partial and avg is not None:
                    status = 'Provisional ' + status.lower()
                monthly.append({
                    'kind': 'Queue', 'month': month, 'terminal': terminal, 'weekday': weekday, 'hour': int(hour),
                    'status': status, 'breach': breach, 'sample_count': len(group),
                    'average_wait': avg, 'average_limit': rule['average_limit'],
                    'pct_over_30': tail, 'tail_limit': rule['tail_limit'] if rule['peak'] else None,
                    'policy': rule['source'], 'partial_month': partial,
                    'detail': f"Average limit {rule['average_limit']} min" + (f"; over-30 limit {rule['tail_limit']}%" if rule['peak'] else '; tail limit applies in policy peak hours only'),
                    'missing_hours': None, 'shortfall_hours': None,
                })
        staffing_findings = []
        for (month, terminal, weekday, hour), group in coverage.groupby(['month', 'terminal_id', 'weekday', 'hour']):
            missing = int(group['missing'].sum())
            shortfalls = int(group.shortfall.sum())
            status = 'Gap recorded' if shortfalls else ('Not assessable' if missing == len(group) else ('Incomplete coverage' if missing else 'Minimum met'))
            staffing_findings.append({
                'kind': 'Staffing', 'month': month, 'terminal': terminal, 'weekday': weekday, 'hour': int(hour),
                'status': status, 'breach': bool(shortfalls), 'sample_count': None,
                'average_wait': None, 'average_limit': None, 'pct_over_30': None, 'tail_limit': None,
                'policy': ' | '.join(group.policy.unique()), 'partial_month': None,
                'detail': f'{shortfalls} observed shortfall hours; {missing} unknown hours; required counters {int(group.minimum.min())}–{int(group.minimum.max())}',
                'missing_hours': missing, 'shortfall_hours': shortfalls,
            })

        wait_chart = []
        heatmap = []
        if not selection.empty:
            for (terminal, hour), group in selection.groupby(['terminal_id', 'immigration_hour']):
                wait_chart.append({'terminal': terminal, 'hour': int(hour), **queue_statistics(group)})
            for (weekday, hour), group in selection.groupby(['weekday', 'immigration_hour']):
                heatmap.append({'weekday': weekday, 'hour': int(hour), **queue_statistics(group)})
        counter_chart = []
        for (terminal, hour), group in coverage.groupby(['terminal_id', 'hour']):
            counter_chart.append({
                'terminal': terminal, 'hour': int(hour), 'average_open': number(group.counters_open.mean()),
                'average_required': number(group.minimum.mean()), 'missing_hours': int(group['missing'].sum()),
                'shortfall_hours': int(group.shortfall.sum()),
            })

        incident_frame = self.query(
            'SELECT incident_id, start_date, start_hour, duration_min, terminal_id, incident_type, severity '
            'FROM incidents WHERE start_date <= ?' + ('' if f.terminal == 'ALL' else " AND terminal_id IN (?, 'ALL')"),
            (f.date_to.isoformat(), *terminal_params),
        )
        selected_hours = set()
        for row in expected:
            selected_hours.add(pd.Timestamp(row['roster_date']) + pd.Timedelta(hours=row['hour']))
        incidents = []
        for incident in rows(incident_frame):
            start = pd.Timestamp(incident['start_date']) + pd.Timedelta(hours=incident['start_hour'])
            end = start + pd.Timedelta(minutes=incident['duration_min'])
            if any(start < hour + pd.Timedelta(hours=1) and end > hour for hour in selected_hours):
                incidents.append({**incident, 'association': 'Time overlap only; cause not established'})

        stats = queue_statistics(selection)
        findings = monthly + staffing_findings
        return {
            'filters': f.model_dump(mode='json'), 'fictional': True, 'metrics': {
                **stats, 'roster_coverage_pct': number(observed_hours / len(coverage) * 100) if len(coverage) else None,
                'expected_roster_hours': len(coverage), 'observed_roster_hours': observed_hours,
                'missing_roster_hours': missing_hours, 'shortfall_hours': int(coverage.shortfall.sum()),
                'queue_breach_groups': sum(item['breach'] for item in monthly),
            },
            'wait_chart': wait_chart, 'counter_chart': counter_chart, 'heatmap': heatmap,
            'findings': findings, 'incidents': incidents,
            'escalations': self.escalations(sample, f), 'exclusions': exclusions,
            'basis': 'Departure immigration; smart-gate users included; checkpoint-hours counted separately per terminal.',
            'data_period': {'from': meta['date_from'], 'to': meta['date_to']},
        }

    @staticmethod
    def escalations(sample: pd.DataFrame, f: Filters) -> list[dict]:
        if sample.empty:
            return []
        grouped = sample.groupby(['terminal_id', 'journey_date', 'immigration_hour']).immigration_wait_min.agg(['mean', 'max']).reset_index()
        events = []
        for terminal, group in grouped.groupby('terminal_id'):
            previous = None
            run = 0
            previous_limit = None
            for row in group.sort_values(['journey_date', 'immigration_hour']).itertuples():
                day = date.fromisoformat(row.journey_date)
                hour = int(row.immigration_hour)
                timestamp = pd.Timestamp(day) + pd.Timedelta(hours=hour)
                limit = 40 if day < date(2026, 1, 1) else 30
                consecutive = previous is not None and timestamp - previous == pd.Timedelta(hours=1) and previous_limit == limit
                run = (run + 1 if consecutive else 1) if row.mean > limit else 0
                previous, previous_limit = timestamp, limit
                if not (f.date_from <= day <= f.date_to and f.hour_from <= hour <= f.hour_to and (f.weekday == 'All' or day.strftime('%A') == f.weekday)):
                    continue
                if run >= 3:
                    source = 'GIA-SUP-12 section 3.3 (historical)' if limit == 40 else 'GIA-POL-01 section 3.3'
                    events.append({'terminal': terminal, 'date': str(day), 'hour': hour, 'level': 'Level 2 / staff request', 'policy': source, 'detail': f'Hourly sampled averages >{limit} min for at least 3 consecutive hours.'})
                if day >= date(2026, 3, 1):
                    if row.mean > 20:
                        events.append({'terminal': terminal, 'date': str(day), 'hour': hour, 'level': 'Level 1', 'policy': 'GIA-SOP-06 section 2.1', 'detail': 'Hourly sampled average >20 min.'})
                    if row.max > 45:
                        events.append({'terminal': terminal, 'date': str(day), 'hour': hour, 'level': 'Level 3', 'policy': 'GIA-SOP-06 section 2.3', 'detail': 'At least one sampled individual wait >45 min; flight eligibility and response require verification.'})
        return events

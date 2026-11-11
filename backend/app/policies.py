"""Explicit date-aware rules and clause retrieval from the provided register."""
from datetime import date, datetime
from pathlib import Path
import csv
import re


def service_rule(day: date, hour: int) -> dict:
    current = day >= date(2026, 1, 1)
    return {
        'average_limit': 15 if current else 20,
        'tail_limit': 5 if current else 10,
        'peak': (17 if current else 18) <= hour <= 22,
        'source': 'GIA-POL-01 §§2.3, 3.1, 4.2' if current else 'GIA-SUP-12 §§1–3 (historically applicable v2.0)',
    }


def counter_rule(day: date, hour: int, terminal: str) -> dict:
    source = 'GIA-STD-03 §§2–3'
    if hour < 5:
        minimum = 4 if terminal == 'T1' else 2
    elif hour < 7:
        minimum = 7 if terminal == 'T1' else 4
    elif terminal == 'T2':
        minimum = 5
    elif 17 <= hour <= 22:
        minimum = 9
        if day.weekday() == 4 and date(2025, 10, 1) <= day <= date(2026, 6, 30):
            minimum = 5
            source = 'GIA-STD-03 §5.2 (EX-2025-07, expires 30 June 2026)'
    else:
        minimum = 8
    return {'minimum': minimum, 'source': source}


class PolicyStore:
    def __init__(self, folder: Path):
        self.clauses = []
        register = folder / 'Document_Register.csv'
        with register.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                text = (folder / row['file']).read_text(encoding='utf-8')
                doc = row['doc_no'].split()[0]
                match_date = re.search(r'\d{1,2} [A-Za-z]+ \d{4}', row['effective'])
                valid_from = datetime.strptime(match_date.group(), '%d %B %Y').date().isoformat() if match_date else None
                for line in text.splitlines():
                    match = re.match(r'^(\d+\.\d+)\s+(.+)', line)
                    if match:
                        self.clauses.append({
                            'document': doc, 'section': match[1],
                            'status': row['status'], 'effective': row['effective'],
                            'effective_from': valid_from,
                            'effective_to': '2025-12-31' if doc == 'GIA-SUP-12' else None,
                            'version': row['version'], 'text': match[2],
                        })

    def search(self, query: str, date_from: date, date_to: date) -> list[dict]:
        words = set(re.findall(r'\w+', query.lower()))
        # Always include the rules needed to interpret historical queue analysis.
        essential = {'GIA-POL-01', 'GIA-STD-03', 'GIA-SOP-06', 'GIA-MAN-08'}
        candidates = []
        for clause in self.clauses:
            doc = clause['document']
            if doc == 'GIA-DRAFT-11':
                continue
            if clause['effective_from'] and date.fromisoformat(clause['effective_from']) > date_to:
                continue
            if doc == 'GIA-SUP-12' and date_from > date(2025, 12, 31):
                continue
            if doc == 'GIA-SOP-06' and date_to < date(2026, 3, 1):
                continue
            score = len(words & set(re.findall(r'\w+', clause['text'].lower())))
            if doc in essential or doc == 'GIA-SUP-12':
                score += 2
            candidates.append((score, clause))
        ranked = sorted(candidates, key=lambda item: item[0], reverse=True)
        selected = [item[1] for item in ranked[:14]]
        # Preserve date/exception/measurement context even for Arabic queries.
        required = {('GIA-POL-01', '2.3'), ('GIA-POL-01', '4.2'), ('GIA-STD-03', '5.2'), ('GIA-STD-03', '5.3')}
        if date_from < date(2026, 1, 1):
            required |= {('GIA-SUP-12', '1.1'), ('GIA-SUP-12', '2.3')}
        for _, clause in candidates:
            if (clause['document'], clause['section']) in required and clause not in selected:
                selected.append(clause)
        return selected

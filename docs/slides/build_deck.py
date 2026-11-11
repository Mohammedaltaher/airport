"""Rebuild DMA-queue-and-staffing.pptx from the screenshots in this folder."""
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parents[2]
SLIDES = Path(__file__).resolve().parent
OUT = ROOT / 'DMA-queue-and-staffing.pptx'

NAVY = RGBColor(0x10, 0x26, 0x3C)
TEAL = RGBColor(0x08, 0x7F, 0x75)
INK = RGBColor(0x1C, 0x33, 0x48)
MUTED = RGBColor(0x5A, 0x70, 0x82)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
CREAM = RGBColor(0xF7, 0xF9, 0xFA)
LINE = RGBColor(0xE2, 0xE8, 0xEE)

W, H = 13.333, 7.5


def run(paragraph, text, size, color, bold=False):
    piece = paragraph.add_run()
    piece.text = text
    piece.font.size = Pt(size)
    piece.font.color.rgb = color
    piece.font.bold = bold
    piece.font.name = 'Calibri'
    return piece


def fill(shape, color):
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()


def rect(slide, left, top, width, height, color):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
    fill(shape, color)
    return shape


def textbox(slide, left, top, width, height, text, size, color, bold=False, align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = None
    paragraph = frame.paragraphs[0]
    paragraph.alignment = align
    run(paragraph, text, size, color, bold)
    return box


def bullets(slide, left, top, width, height, items, size=18):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    for index, item in enumerate(items):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = PP_ALIGN.LEFT
        paragraph.space_after = Pt(12)
        paragraph.level = 0
        run(paragraph, item, size, INK)
    return box


def picture(slide, path, left, top, width, height):
    image = Image.open(path)
    ratio = image.width / image.height
    box = width / height
    if ratio > box:
        draw_w, draw_h = width, width / ratio
    else:
        draw_h, draw_w = height, height * ratio
    slide.shapes.add_picture(str(path), Inches(left + (width - draw_w) / 2), Inches(top + (height - draw_h) / 2), Inches(draw_w), Inches(draw_h))


def content_chrome(slide, eyebrow, title):
    rect(slide, 0, 0, W, H, CREAM)
    rect(slide, 0, 0, W, 0.08, TEAL)
    textbox(slide, 0.55, 0.28, 10, 0.28, eyebrow, 12, TEAL, True)
    textbox(slide, 0.55, 0.52, 12, 0.55, title, 30, NAVY, True)


def build():
    deck = Presentation()
    deck.slide_width = Inches(W)
    deck.slide_height = Inches(H)
    blank = deck.slide_layouts[6]

    slide = deck.slides.add_slide(blank)
    rect(slide, 0, 0, W, H, NAVY)
    rect(slide, 0, 0, 0.16, H, TEAL)
    textbox(slide, 0.85, 2.15, 11, 0.3, 'TEAM DMA', 14, TEAL, True)
    textbox(slide, 0.85, 2.55, 11, 1.4, 'Airport queue and staffing', 44, WHITE, True)
    textbox(slide, 0.85, 4.15, 9.5, 0.9, 'Historical evidence, and the policy that applied, in one place.', 22, RGBColor(0xC5, 0xD7, 0xE2))
    textbox(slide, 0.85, 6.7, 10, 0.3, 'Fictional training data  ·  Historical analysis', 14, RGBColor(0x8E, 0xA8, 0xB8))

    slide = deck.slides.add_slide(blank)
    content_chrome(slide, 'THE PROBLEM', 'The records do not answer the question')
    textbox(slide, 0.55, 1.3, 12, 0.8, 'A manager cannot tell, from the raw records, whether a queue or the counters open that hour broke the rule in force that day.', 18, INK)
    bullets(slide, 0.55, 2.25, 12, 4.4, [
        'On 1 January 2026 the waiting-time target changed: 20 minutes and a 10% tail, then 15 minutes and 5%.',
        'Counter minimums are a separate rule. Terminal 1 Fridays allowed five counters until 30 June 2026, then nine.',
        'Passenger journeys are a 4% sample. A missing roster hour is unknown, not zero.',
        'An incident that overlaps a queue does not prove the cause.',
    ])

    slide = deck.slides.add_slide(blank)
    content_chrome(slide, 'THE APPROACH', 'Judge the period with the rule that applied')
    bullets(slide, 0.55, 1.45, 12, 5.2, [
        '1    Choose the terminal and the period.',
        '2    Apply the date-correct rule in code, not inside a model prompt.',
        '3    Show the averages, where the queue builds, and each monthly finding with its policy reference.',
        '4    Ask in English or Arabic. Show the clauses, and save the conversation for follow-up.',
        '5    Leave the roster unchanged. A manager still decides.',
    ], size=20)

    shots = [
        ('THE SELECTED PERIOD', 'September 2026, Terminal 1, 17:00–22:59',
         'Averages and the share over 30 minutes are not scaled. The affected-passenger count is the sample times 25.',
         SLIDES / '01-metrics.png'),
        ('WHERE THE QUEUE BUILDS', 'A darker cell is a longer average, not a breach',
         'Selecting a cell narrows the review to that weekday and hour. The line is the average wait, not a pass or fail.',
         SLIDES / '02-patterns.png'),
        ('WHAT THE POLICY SAYS', 'Each row is one month, terminal, weekday and hour',
         'The status is judged against the rule in force then. The policy reference sits on the row.',
         SLIDES / '03-findings.png'),
    ]
    for eyebrow, title, caption, path in shots:
        slide = deck.slides.add_slide(blank)
        content_chrome(slide, eyebrow, title)
        textbox(slide, 0.55, 1.15, 12.2, 0.55, caption, 15, MUTED)
        picture(slide, path, 0.45, 1.75, 12.4, 5.4)

    slide = deck.slides.add_slide(blank)
    content_chrome(slide, 'ASK, WITH THE SOURCES', 'A question stays inside the selected filters')
    textbox(slide, 0.55, 1.4, 6.3, 2.2, 'Gemini answers from the verified figures and the retrieved clauses. The exchange is saved with those filters and the sources it used.', 20, INK)
    bullets(slide, 0.55, 3.6, 6.3, 2.8, [
        'English and Arabic, including a follow-up.',
        'The clauses used are shown with the answer.',
        'Nothing here changes the roster.',
    ], size=18)
    picture(slide, SLIDES / '04-assistant.png', 7.35, 1.25, 5.5, 5.9)

    slide = deck.slides.add_slide(blank)
    content_chrome(slide, 'THE WORKSPACE', 'Saved chats reopen with their filters')
    textbox(slide, 0.55, 1.15, 12.2, 0.7, 'Expand Ask DMA for the full-screen workspace. Reopening a conversation restores its last scope. A follow-up keeps the last six exchanges and retrieves fresh evidence.', 15, MUTED)
    picture(slide, SLIDES / '05-workspace.png', 0.45, 1.9, 12.4, 5.25)

    deck.save(OUT)
    print(OUT)


if __name__ == '__main__':
    build()

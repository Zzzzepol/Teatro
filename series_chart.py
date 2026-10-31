"""Draw a series' episode ratings as a season-by-season grid."""
from datetime import date
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

SEASONS_PER_PAGE = 8
EPISODES_PER_PAGE = 30
PALETTE = [(9.5, '#32baff'), (9, '#44c882'), (8, '#94db7d'),
           (7, '#f1d96c'), (6, '#efa65b'), (5, '#ef737d'), (0, '#a779ba')]


def episode_score(episode, today):
    try:
        aired = date.fromisoformat(episode.get('air_date') or '')
    except ValueError:
        return None
    score = episode.get('vote_average')
    if aired > today or not episode.get('vote_count') or not isinstance(score, (int, float)):
        return None
    return float(score) if 0 <= score <= 10 else None


def font(size, bold=False):
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def fit(draw, text, face, width):
    text = str(text)
    if draw.textlength(text, font=face) <= width:
        return text
    while text and draw.textlength(text + '…', font=face) > width:
        text = text[:-1]
    return text + '…'


def render_chart(show, seasons, today, poster=None, episode_page=1):
    """Return a PNG. Missing and unaired episodes never receive a made-up score."""
    first = (episode_page - 1) * EPISODES_PER_PAGE + 1
    last = first + EPISODES_PER_PAGE - 1
    episode_maps = [{e['episode_number']: e for e in season.get('episodes', [])
                     if isinstance(e.get('episode_number'), int)} for season in seasons]
    numbers = sorted({n for episodes in episode_maps for n in episodes if first <= n <= last})
    rows = list(range(first, max(numbers) + 1)) if numbers else []
    sidebar, cell_w, cell_h = 260, 92, 38
    width = max(900, sidebar + 65 + len(seasons) * cell_w + 32)
    grid_y = 145
    height = max(620, grid_y + len(rows) * cell_h + 140)
    image = Image.new('RGB', (width, height), '#101216')
    draw = ImageDraw.Draw(image)
    title_font, regular, small, score_font = font(27, True), font(19), font(14), font(21, True)
    draw.text((26, 20), fit(draw, show.get('name', 'Series'), title_font, width - 52), font=title_font, fill='#f3f4f7')
    draw.text((26, 62), 'EPISODE RATINGS  /  TMDB COMMUNITY SCORES', font=small, fill='#a9b0bf')
    for index, (threshold, color) in enumerate(PALETTE):
        x = 26 + index * 120
        draw.rounded_rectangle((x, 98, x + 13, 111), radius=4, fill=color)
        label = ['9.5–10', '9.0–9.4', '8.0–8.9', '7.0–7.9', '6.0–6.9', '5.0–5.9', '<5.0'][index]
        draw.text((x + 20, 94), label, font=small, fill='#c4cad5')
    if poster:
        try:
            with Image.open(BytesIO(poster)) as source:
                thumb = ImageOps.fit(source.convert('RGB'), (208, 290))
                image.paste(thumb, (26, grid_y))
        except (OSError, ValueError, Image.DecompressionBombError):
            poster = None
    if not poster:
        draw.rounded_rectangle((26, grid_y, 234, grid_y + 290), radius=12, fill='#20252e')
        draw.text((57, grid_y + 130), 'TEATRO SERIES', font=small, fill='#a9b0bf')
    year = (show.get('first_air_date') or '')[:4] or 'Year unknown'
    draw.text((26, grid_y + 306), year + ' · ' + show.get('status', 'Status unknown'), font=small, fill='#b5bdca')
    rating = show.get('vote_average')
    rating_text = f"Series: {rating:.1f}/10" if isinstance(rating, (int, float)) and show.get('vote_count') else 'Series score: N/A'
    draw.text((26, grid_y + 335), rating_text, font=regular, fill='#f1d96c')
    draw.text((26, grid_y + 365), f"{show.get('vote_count', 0):,} series votes", font=small, fill='#a9b0bf')
    x0 = sidebar + 55
    for col, season in enumerate(seasons):
        draw.text((x0 + col * cell_w + 22, grid_y - 25), f"S{season['season_number']}", font=small, fill='#dce1eb')
    for row, number in enumerate(rows):
        y = grid_y + row * cell_h
        draw.text((sidebar, y + 7), f'E{number}', font=small, fill='#a9b0bf')
        for col, episodes in enumerate(episode_maps):
            episode = episodes.get(number)
            if episode is None:
                continue
            score = episode_score(episode, today)
            shown = round(score, 1) if score is not None else None
            color = next(color for minimum, color in PALETTE if shown >= minimum) if shown is not None else '#303744'
            x = x0 + col * cell_w
            draw.rounded_rectangle((x, y, x + cell_w - 7, y + cell_h - 5), radius=5, fill=color)
            label = f'{shown:.1f}' if shown is not None else 'N/A'
            draw.text((x + (cell_w - 7 - draw.textlength(label, font=score_font)) / 2, y + 4), label,
                      font=score_font, fill='#101216' if shown is not None else '#a9b0bf')
    avg_y = grid_y + len(rows) * cell_h + 14
    draw.text((sidebar - 5, avg_y + 3), 'AVG', font=small, fill='#a9b0bf')
    for col, episodes in enumerate(episode_maps):
        scores = [score for episode in episodes.values() if (score := episode_score(episode, today)) is not None]
        label = f'{sum(scores) / len(scores):.1f}' if scores else 'N/A'
        draw.text((x0 + col * cell_w + 20, avg_y), label, font=regular, fill='#e0e6ef')
    footer_y = max(grid_y + 410, avg_y + 48)
    draw.text((26, footer_y), f'As of {today:%d %b %Y} · N/A = unaired, undated or unrated · Blank = no episode', font=small, fill='#a9b0bf')
    draw.text((26, footer_y + 25), 'AVG = mean of all rated, aired episodes in that season · Not IMDb ratings', font=small, fill='#a9b0bf')
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()

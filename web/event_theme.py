import re


THEME_RULES = (
    ("🏃", r"\b(?:run|running|runner|runners|jog|jogging|marathon|5k|10k|half marathon)\b"),
    ("🏋️", r"\b(?:lifting|weightlifting|powerlifting|strength training|workout|barbell|deadlift|squat|gym)\b"),
    ("📸", r"\b(?:photo|photos|photograph|photographs|photography|photographer|photo[- ]?walk|cyanotype|darkroom)\b"),
    ("💃", r"\b(?:dance|dancing|dance-off|dance-offs|salsa|bachata|tango|ballroom|ballet|rave)\b"),
    ("📚", r"\b(?:book|books|bookish|book club|reading|literary|literature|poetry|poem|zine|journal club)\b"),
    ("✍️", r"\b(?:writing|writer|writers|journaling|journal|storytelling|calligraphy)\b"),
    ("🎨", r"\b(?:art|artist|gallery|museum|painting|drawing|collage|craft|crafts|clay|ceramic|cross[- ]?stitch|scrapbook|jewelry|beading|fiber art|sculpt)\b"),
    ("🎬", r"\b(?:film|cinema|movie|screening|documentary|animation|anime|cartoon|35mm|nitehawk|metrograph)\b"),
    ("🎤", r"\b(?:karaoke|open mic|spoken word|talent show)\b"),
    ("🎵", r"\b(?:music|concert|jazz|band|dj|choir|orchestra|song|listening party)\b"),
    ("🎲", r"\b(?:game|games|gaming|trivia|bingo|chess|board game|puzzle)\b"),
    ("⚽", r"\b(?:sport|sports|soccer|football|basketball|baseball|softball|volleyball|tennis|pickleball|pétanque|petanque)\b"),
    ("🧘", r"\b(?:yoga|meditation|breathwork|mindfulness|sound bath)\b"),
    ("🍷", r"\b(?:tasting|wine|wines|cocktail|cocktails|spirits|sake|brewery|beer)\b"),
    ("🍽️", r"\b(?:dinner|brunch|lunch|supper|food|cooking|baking|bread|meal|restaurant|munchies)\b"),
    ("🚶", r"\b(?:walk|walking|hike|hiking|stroll|outdoor tour|gallery hop)\b"),
    ("🪄", r"\b(?:magic|magician|illusion)\b"),
    ("🎭", r"\b(?:theater|theatre|comedy|improv|performance|live show)\b"),
    ("💬", r"\b(?:philosophy|discussion|conversation|talk|debate|lecture|networking)\b"),
    ("🎉", r"\b(?:party|social|mixer|meetup|happy hour|singles|dating|community gathering)\b"),
)

COMPILED_THEME_RULES = tuple(
    (emoji, re.compile(pattern, re.IGNORECASE)) for emoji, pattern in THEME_RULES
)


def event_theme_emoji(event):
    """Return one stable visual theme cue from an event's existing text."""
    parts = [
        event.get("title"),
        event.get("host"),
        event.get("description"),
        event.get("venue"),
        event.get("neighborhood"),
        event.get("source_id"),
    ]
    tags = event.get("format_tags") or []
    if isinstance(tags, (list, tuple, set)):
        parts.extend(tags)
    else:
        parts.append(tags)
    text = " ".join(str(part) for part in parts if part)
    for emoji, pattern in COMPILED_THEME_RULES:
        if pattern.search(text):
            return emoji
    return "✨"

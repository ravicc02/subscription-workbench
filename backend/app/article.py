"""Article extraction reused from market-news-agent-harness, including RTHK br handling."""

import re

BYLINE = re.compile(r"^(?:編輯|编辑|責任編輯|责任编辑|责编)\s*[：:]|^Edited by\b", re.I)


def clean_body(raw, source):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "lxml")
    links = [a.get("href") for a in soup.select("a[href]")]
    restricted = bool(soup.select_one('[data-test="paywall"], .paywall, .paywall-overlay'))
    for node in soup.select(
        "script, style, nav, header, footer, aside, .mega-menu, .mega-menu-overlay"
    ):
        node.decompose()
    container = next(
        (
            soup.select_one(selector)
            for selector in source["selectors"]
            if soup.select_one(selector)
        ),
        None,
    )
    paragraphs = []
    if container:
        if source.get("paragraph_mode") == "br":
            # RTHK: the body container holds bare text separated by <br>, no <p> tags.
            blocks = []
            for child in container.children:
                name = getattr(child, "name", None)
                if name == "br":
                    blocks.append("\n")
                elif name is None:
                    blocks.append(str(child))
                else:
                    blocks.append(" " + child.get_text(" ", strip=True) + " ")
            for part in re.split(r"\n+", "".join(blocks)):
                text = " ".join(part.split())
                if len(text) < source.get("min_paragraph_characters", 40):
                    continue
                if BYLINE.match(text) or text in paragraphs:
                    continue
                paragraphs.append(text)
        else:
            tags = ["p", "h2", "h3", "li"] + (["tr"] if source.get("include_tables") else [])
            for element in container.find_all(tags):
                if element.name == "tr":
                    cells = element.find_all(["th", "td"], recursive=False)
                    text = " | ".join(
                        " ".join(cell.get_text(" ", strip=True).split()) for cell in cells
                    )
                else:
                    text = " ".join(element.get_text(" ", strip=True).split())
                if (
                    len(text) >= source.get("min_paragraph_characters", 40)
                    and text not in paragraphs
                ):
                    paragraphs.append(text)
    good = len(paragraphs) >= source.get("min_body_paragraphs", 3) and sum(
        map(len, paragraphs)
    ) >= source.get("min_body_characters", 500)
    return (
        paragraphs,
        links,
        ("partial_text" if restricted else "full_text") if good else None,
    )

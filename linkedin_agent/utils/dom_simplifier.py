from bs4 import BeautifulSoup
import re

def simplify_html(html_content: str) -> str:
    """
    Cleans raw HTML by removing scripts, styles, SVGs, and unnecessary attributes
    so that an LLM can parse it efficiently without exceeding token limits.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # Remove non-structural tags
    for tag in soup(["script", "style", "svg", "path", "noscript", "meta", "link", "br"]):
        tag.decompose()

    # Allowed attributes that help identify elements
    allowed_attributes = {"id", "class", "name", "aria-label", "role", "href", "type", "placeholder"}

    # Clean attributes from all remaining tags
    for tag in soup.find_all(True):
        attrs = dict(tag.attrs)
        for attr in attrs:
            if attr not in allowed_attributes:
                del tag[attr]
        
        # Flatten class lists to raw strings for easier reading
        if "class" in tag.attrs:
            if isinstance(tag["class"], list):
                tag["class"] = " ".join(tag["class"])

    # Remove empty tags (recursively, starting from bottom-up)
    for tag in reversed(soup.find_all(True)):
        if not tag.contents and not tag.attrs:
            tag.decompose()

    # Minify text and whitespace
    clean_html = str(soup)
    clean_html = re.sub(r'\n+', '\n', clean_html)
    clean_html = re.sub(r' +', ' ', clean_html)
    
    return clean_html.strip()

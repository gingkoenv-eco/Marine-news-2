"""
fetch_news.py

Fetches marine/environmental news from a set of RSS feeds, filters and
categorizes the articles, and writes the result to news.json.

This script is meant to be run on a schedule (see .github/workflows/update-news.yml)
rather than on every page load. The front-end (index.html) simply fetches the
resulting news.json file, so visitors never wait on RSS parsing.
"""

import calendar
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OUTPUT_FILE = "news.json"

# Some sites (e.g. Oceanographic Magazine) reject requests that don't look like
# they're coming from a real browser, and silently return nothing rather than
# an error - so we always send a realistic User-Agent / Accept header.
FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.7",
}


def build_keyword_pattern(words):
    """Compile a case-insensitive, whole-word-or-phrase regex from a keyword
    list. Using \\b word boundaries (instead of a plain substring check)
    prevents false positives like "eu" matching inside "neutral", "ray"
    matching inside "array", or "sea" matching inside "season" or "disease".
    An optional trailing "s" is allowed so common regular plurals (e.g.
    "sharks", "reefs") still match a singular keyword - this won't catch
    irregular plurals (e.g. "fishery" -> "fisheries"), but those are mostly
    already covered by separate phrase entries in the keyword lists. Being
    case-insensitive also means keywords like "MPA" or "NBS" work correctly
    even though the text they're checked against is lowercased."""
    escaped = sorted((re.escape(w) for w in words if w), key=len, reverse=True)
    pattern = r"\b(?:" + "|".join(escaped) + r")s?\b"
    return re.compile(pattern, re.IGNORECASE)

# List of RSS sources - STRICTLY MARINE-FOCUSED
sources = [
    # International Marine News Outlets
    {"name": "The Guardian Ocean & Marine", "url": "https://www.theguardian.com/environment/oceans/rss", "type": "news", "language": "en"},
    {"name": "BBC Ocean & Marine", "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "type": "news", "language": "en"},
    {"name": "The Conversation FR", "url": "https://theconversation.com/fr/environnement/articles.atom", "type": "news", "language": "fr"},
    {"name": "The Conversation UK", "url": "https://theconversation.com/uk/environment/articles.atom", "type": "news", "language": "en"},
    {"name": "The Conversation US", "url": "https://theconversation.com/us/environment/articles.atom", "type": "news", "language": "en"},
    {"name": "Carbonbrief", "url": "https://www.carbonbrief.org/feed/", "type": "news", "language": "en"},
    {"name": "Climate change news", "url": "https://www.climatechangenews.com/feed/", "type": "news", "language": "en"},

    {"name": "Reporterre", "url": "https://reporterre.net/spip.php?page=backend-simple", "type": "news", "language": "fr"},
    {"name": "Oceanographic", "url": "https://oceanographicmagazine.com/news/feed/", "type": "news", "language": "en"},

    # French Marine-Focused Sources
    {"name": "Le Monde Planète", "url": "https://www.lemonde.fr/planete/rss_full.xml", "type": "news", "language": "fr"},
    {"name": "Vert Eco Articles", "url": "https://vert.eco/tous-les-articles", "type": "news", "language": "fr"},
    {"name": "Le Monde Environnement", "url": "https://www.lemonde.fr/environnement/rss_full.xml", "type": "news", "language": "fr"},
    {"name": "The Conversation", "url": "https://theconversation.com/articles.atom?language=en", "type": "news", "language": "en"},
    # Marine NGOs & International Organizations
    {"name": "Ocean Conservancy", "url": "https://oceanconservancy.org/feed/", "type": "news", "language": "en"},

    # Specialized Marine Media
    {"name": "Mongabay Marine", "url": "https://news.mongabay.com/feed/", "type": "news", "language": "en"},
]

# Keywords for filtering articles - STRICTLY MARINE-FOCUSED
keywords = [
    # Core Marine Terms
    "marine", "ocean", "sea", "aquatic", "pelagic", "littoral",
    "coral reef", "kelp forest", "seagrass", "mangrove", "eelgrass", "saltmarsh",
    "estuary", "coastal", "coast", "beach", "shore", "bay", "gulf", "lagoon",
    "deep sea", "abyssal", "benthic", "mesopelagic", "bathypelagic", "seabed",

    # Marine Biodiversity & Species
    "marine biodiversity", "fish", "whale", "dolphin", "seal", "sea lion",
    "shark", "ray", "sea turtle", "seabird", "penguin",
    "coral", "anemone", "sponge", "mollusk", "crab", "lobster", "shrimp", "oyster",
    "plankton", "krill", "zooplankton", "phytoplankton",

    # Fisheries & Marine Resources
    "fishery", "fishing", "fish stock", "overfishing", "sustainable fishing", "bycatch",
    "fishing pressure", "marine resource", "fisheries management",
    "aquaculture", "shellfish farming",

    # Pollution & Marine Health
    "plastic pollution", "ocean plastic", "microplastic", "marine debris",
    "ocean pollution", "marine pollution", "chemical pollution",
    "oil spill", "toxic contamination",
    "dead zone", "hypoxia", "acidification", "ocean acidification",

    # Coastal and High Seas Carbon Cycling
    "carbon cycling", "coastal carbon", "high seas carbon", "blue carbon",
    "carbon sequestration", "ocean carbon", "marine carbon",
    "carbon flux", "chlorophyll", "primary production", "ocean productivity",
    "upwelling", "stratification", "thermocline",

    # Climate impacts on marine systems
    "ocean warming", "marine heat wave", "sea temperature",
    "sea level rise", "deoxygenation",

    # Nature-Based Solutions in Marine Environment
    "nature-based solution", "NBS", "ecosystem-based adaptation",
    "mangrove restoration", "seagrass restoration", "coral restoration",
    "marine protected area", "MPA", "marine reserve", "marine sanctuary",
    "no-take zone", "marine spatial planning", "blue economy",

    # Fisheries-Climate Ecosystem Approach
    "fisheries climate", "seafood security", "ecosystem-based fisheries",
    "community-based management", "indigenous marine", "fishing communities",

    # Ocean Health & Human Well-being
    "ocean health", "marine health", "nutritional security",
    "coastal livelihoods", "marine livelihoods", "vulnerable population",
    "social-ecological system", "socio-ecological resilience",

    # Mesophotic & Deep Ocean Ecosystems
    "mesophotic", "twilight zone", "deep sea ecosystem",
    "hadal zone", "trench", "hydrothermal vent", "cold seep",
    "bioluminescence", "deep sea biodiversity",
    "deep sea mining", "mineral extraction",

    # Vulnerable Marine Socio-Ecological Systems & Climate Resilience
    "polar ocean", "arctic", "antarctica", "sea ice",
    "coral bleaching", "reef resilience", "reef recovery",
    "vulnerable ecosystem", "adaptation capacity", "resilience",

    # Migration & Habitat Changes
    "migration", "migratory species", "marine migration",
    "habitat change", "range shift", "species distribution",
    "invasive species", "connectivity", "larval dispersal"
]

keywords += [
    # French keyword translations for marine themes
    "mer", "océan", "océans", "aquatique", "pélagique", "littoral",
    "récif corallien", "forêt de varech", "herbier", "mangrove", "zostère", "marais salant",
    "estuaire", "côtier", "côte", "plage", "rivage", "baie", "golfe", "lagon",
    "mer profonde", "abyssal", "benthique", "mésopélagique", "bathypélagique", "fond marin",
    "biodiversité marine", "poisson", "baleine", "dauphin", "phoque", "otarie",
    "requin", "raie", "tortue de mer", "oiseau marin", "manchot",
    "corail", "anémone", "éponge", "mollusque", "crabe", "homard", "crevette", "huître",
    "plancton", "krill", "zooplancton", "phytoplancton",
    "pêche", "pêche durable", "surpêche", "stock de poissons", "captures accessoires",
    "pression de pêche", "ressource marine", "gestion des pêches",
    "aquaculture", "élevage de coquillages",
    "pollution plastique", "plastique océanique", "microplastique", "débris marins",
    "pollution des océans", "pollution marine", "pollution chimique",
    "marée noire", "contamination toxique",
    "zone morte", "hypoxie", "acidification", "acidification des océans",
    "cycle du carbone", "carbone côtier", "carbone en haute mer", "carbone bleu",
    "séquestration du carbone", "carbone océanique", "carbone marin",
    "flux de carbone", "chlorophylle", "production primaire", "productivité océanique",
    "upwelling", "stratification", "thermocline",
    "réchauffement des océans", "canicule marine", "température de la mer",
    "élévation du niveau de la mer", "désoxygénation",
    "solution fondée sur la nature", "solution basée sur la nature", "adaptation fondée sur les écosystèmes",
    "restauration de mangroves", "restauration des herbiers", "restauration des coraux",
    "aire marine protégée", "AMP", "réserve marine", "sanctuaire marin",
    "zone sans pêche", "planification spatiale marine", "économie bleue",
    "pêcheries climat", "sécurité alimentaire", "pêches basées sur les écosystèmes",
    "gestion communautaire", "marine autochtone", "communautés de pêcheurs",
    "santé des océans", "santé marine", "sécurité nutritionnelle",
    "moyens de subsistance côtiers", "moyens de subsistance marins", "population vulnérable",
    "système socio-écologique", "résilience socio-écologique",
    "mésophotique", "zone crépusculaire", "écosystème des grands fonds",
    "zone hadale", "fosse", "source hydrothermale", "suintement froid",
    "bioluminescence", "biodiversité des grands fonds",
    "exploitation minière des grands fonds", "extraction minière",
    "océan polaire", "arctique", "antarctique", "glace de mer",
    "blanchissement des coraux", "résilience des récifs", "récupération des récifs",
    "écosystème vulnérable", "capacité d'adaptation", "résilience",
    "migration", "espèces migratrices", "migration marine",
    "changement d'habitat", "déplacement d'aire de répartition", "distribution des espèces",
    "espèces invasives", "connectivité", "dispersion larvaire"
]

# Keywords for broader non-ocean topics that should only appear in Other News
broader_keywords = [
    "renewable", "renewables", "clean energy", "green energy", "solar", "wind", "hydro", "geothermal", "energy transition",
    "electric vehicle", "EV", "battery", "grid", "storage", "power plant", "offshore wind",
    "climate", "climate change", "global warming", "carbon", "emission", "net zero", "decarbonization",
    "environment", "environmental", "sustainability", "sustainable development", "green economy",
    "pollution", "air pollution", "water pollution", "waste", "recycling", "circular economy",
    "biodiversity", "ecosystem", "conservation", "nature", "forestry", "land use",
    "policy", "governance", "regulation", "legislation", "international agreement", "treaty", "COP"
]

broader_keywords += [
    # French translations for broader environmental topics
    "renouvelable", "énergies renouvelables", "énergie propre", "énergie verte", "solaire", "éolien", "hydroélectrique", "géothermie", "transition énergétique",
    "véhicule électrique", "VE", "batterie", "réseau", "stockage", "centrale électrique", "éolienne offshore",
    "climat", "changement climatique", "réchauffissement climatique", "carbone", "émission", "zéro net", "décarbonisation",
    "environnement", "environnemental", "durabilité", "développement durable", "économie verte",
    "pollution", "pollution de l'air", "pollution de l'eau", "déchets", "recyclage", "économie circulaire",
    "biodiversité", "écosystème", "conservation", "nature", "foresterie", "utilisation des terres",
    "politique", "gouvernance", "réglementation", "législation", "accord international", "traité", "COP"
]

KEYWORD_PATTERN = build_keyword_pattern(keywords)
BROADER_KEYWORD_PATTERN = build_keyword_pattern(broader_keywords)

# Keyword lists used for categorization and clustering - moved to module level
# and precompiled once (rather than rebuilt on every article) for both speed
# and consistent word-boundary matching.
MULTIMEDIA_WORDS = ["video", "podcast", "watch", "listen", "documentary", "interview", "webinar", "report", "white paper"]
RESEARCH_SOURCE_WORDS = ["nature", "science daily", "pubmed", "research"]
POLICY_WORDS = [
    "policy", "politics", "legislation", "regulation", "governance", "agreement", "treaty",
    "negotiation", "government", "parliament", "congress", "senate", "eu", "european",
    "international", "cop", "climate summit", "environmental law", "sustainability goal",
    "net zero", "commitment"
]

MULTIMEDIA_PATTERN = build_keyword_pattern(MULTIMEDIA_WORDS)
RESEARCH_SOURCE_PATTERN = build_keyword_pattern(RESEARCH_SOURCE_WORDS)
POLICY_PATTERN = build_keyword_pattern(POLICY_WORDS)

THEME_KEYWORDS = {
    "Plastic & Ocean Pollution": ["plastic", "pollution", "microplastic", "marine debris", "contamination"],
    "Marine Biodiversity & Conservation": ["biodiversity", "species", "habitat", "extinction", "endangered", "whale", "shark", "coral"],
    "Sustainable Fisheries": ["fishery", "fishing", "fish stock", "overfishing", "bycatch", "seafood"],
    "Blue Carbon & Carbon Cycling": ["carbon cycling", "blue carbon", "carbon sequestration", "ocean carbon", "carbon flux", "primary production"],
    "Nature-Based Solutions & Restoration": ["nature-based solution", "restoration", "mangrove", "seagrass", "reef restoration", "ecosystem-based"],
    "Fisheries & Climate Change Ecosystem": ["fisheries climate", "ecosystem-based fisheries", "community-based management", "food security"],
    "Ocean Health & Human Well-being": ["ocean health", "coastal livelihoods", "vulnerable population", "nutritional security", "socio-ecological"],
    "Deep Ocean & Mesophotic Ecosystems": ["deep sea", "mesophotic", "twilight zone", "hadal", "abyssal", "hydrothermal vent", "bioluminescence"],
    "Polar & Reef Ecosystems & Resilience": ["polar", "arctic", "sea ice", "coral bleaching", "reef resilience", "thermal resilience", "vulnerable ecosystem"],
    "Species Migration & Habitat Change": ["migration", "migratory", "range shift", "species distribution", "invasive species", "larval dispersal"],
    "Marine Protected Areas & Policy": ["marine protected area", "MPA", "marine reserve", "marine spatial planning", "policy", "governance"],
}
THEME_PATTERNS = {theme: build_keyword_pattern(words) for theme, words in THEME_KEYWORDS.items()}


def normalize_title(title):
    """Normalize a title for duplicate detection: decode HTML entities,
    normalize curly quotes, collapse whitespace, lowercase."""
    text = html.unescape(title or "")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def is_near_duplicate_title(normalized_title, seen_titles):
    """Check a normalized title against previously seen ones. Catches exact
    matches plus near-duplicates where one feed appends a short suffix like
    " | Mongabay" or " - The Guardian" to an otherwise identical title,
    regardless of how long the base title is."""
    if normalized_title in seen_titles:
        return True

    for prev in seen_titles:
        shorter, longer = (normalized_title, prev) if len(normalized_title) <= len(prev) else (prev, normalized_title)
        if len(shorter) >= 25 and longer.startswith(shorter) and (len(longer) - len(shorter)) <= 30:
            return True

    return False


def extract_image_url(entry):
    """Try to find a cover image for a feed entry. Checks the common
    feedparser-normalized fields first (media:content, media:thumbnail,
    enclosures), then falls back to pulling the first <img> tag out of the
    raw HTML summary/description. Returns None if nothing is found - not
    every feed provides one."""
    for field in ("media_content", "media_thumbnail"):
        media_list = entry.get(field)
        if media_list:
            for media in media_list:
                url = media.get("url")
                if url:
                    return url

    for enclosure in entry.get("enclosures", []) or []:
        enclosure_type = enclosure.get("type", "")
        url = enclosure.get("href") or enclosure.get("url")
        if url and enclosure_type.startswith("image"):
            return url

    raw_html = entry.get("summary") or entry.get("description") or ""
    if raw_html:
        try:
            soup = BeautifulSoup(raw_html, "html.parser")
            img = soup.find("img")
            if img and img.get("src"):
                return img["src"]
        except Exception:
            pass

    return None


def fetch_feed(url):
    """Fetch and parse a feed, using realistic browser headers.

    Some sites block or silently ignore requests that don't look like they're
    coming from a real browser - feedparser's own default request has no such
    headers, so we fetch the raw bytes ourselves first and only fall back to
    letting feedparser fetch it directly if that fails.
    """
    try:
        response = requests.get(url, headers=FEED_HEADERS, timeout=15)
        response.raise_for_status()
        return feedparser.parse(response.content)
    except Exception as e:
        logger.debug(f"Direct request failed for {url} ({e}); falling back to feedparser's own fetch")
        try:
            return feedparser.parse(url, request_headers=FEED_HEADERS)
        except Exception as e2:
            logger.warning(f"feedparser fallback also failed for {url}: {e2}")
            return feedparser.parse("")  # returns an empty/falsy feed object


def get_published_datetime(entry):
    """Extract a publish date from a feed entry, trying several field names
    and formats before giving up - this avoids silently defaulting every
    article from a source to "now" just because the date field it uses is
    named differently (e.g. 'updated' instead of 'published')."""

    # Textual fields, parsed with dateutil (handles most RSS/Atom date formats)
    for field in ("published", "updated", "created", "pubDate"):
        value = entry.get(field)
        if not value:
            continue
        try:
            dt = date_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue

    # feedparser-normalized struct_time fields (more reliable when the raw
    # string format is unusual, since feedparser has already parsed it)
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(field)
        if struct:
            try:
                return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
            except Exception:
                continue

    return None


def discover_feed_url(page_url):
    """Discover RSS/Atom feed URL from a page by parsing link tags."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsAggregator/1.0)"}
        response = requests.get(page_url, headers=headers, timeout=10)
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        candidates = []
        for link in soup.find_all("link", rel=["alternate", "alternate stylesheet"]):
            href = link.get("href")
            type_attr = link.get("type", "")
            if href and ("rss" in type_attr or "atom" in type_attr or href.endswith(".xml") or href.endswith(".rss") or href.endswith(".atom")):
                candidates.append(href)

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "rss" in href or "atom" in href or href.endswith(".xml"):
                candidates.append(href)

        for href in candidates:
            feed_url = urljoin(page_url, href)
            feed = fetch_feed(feed_url)
            if getattr(feed, 'entries', None):
                logger.info(f"Discovered RSS/Atom feed for {page_url}: {feed_url}")
                return feed_url
    except Exception as e:
        logger.debug(f"Feed discovery failed for {page_url}: {e}")
    return None


def fetch_articles():
    """Fetch articles from RSS feeds with error handling"""
    articles = []
    seen_links = set()
    seen_titles = set()
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)

    for source in sources:
        try:
            logger.info(f"Fetching from {source['name']}")
            feed = fetch_feed(source["url"])

            if not feed.entries:
                logger.info(f"No feed entries for {source['name']}; attempting feed discovery")
                discovered = discover_feed_url(source["url"])
                if discovered:
                    feed = fetch_feed(discovered)
                else:
                    logger.warning(f"No entries and no feed discovered for {source['name']}")

            if not getattr(feed, 'entries', None):
                continue

            for entry in feed.entries:
                try:
                    published = get_published_datetime(entry)
                    if published is None:
                        logger.debug(f"No parsable date for entry from {source['name']}; using current time")
                        published = datetime.now(timezone.utc)

                    if published < cutoff_date:
                        continue

                    title = entry.get('title', 'No title')
                    link = entry.get('link', '')
                    summary = entry.get('summary', '')

                    if not link:
                        continue

                    image_url = extract_image_url(entry)

                    summary = re.sub('<[^<]+?>', '', summary)[:150]
                    summary = summary.strip()

                    text = (title + " " + summary).lower()

                    marine_match = bool(KEYWORD_PATTERN.search(text))
                    broad_match = bool(BROADER_KEYWORD_PATTERN.search(text))
                    if not marine_match and not broad_match:
                        continue

                    if link in seen_links:
                        continue

                    normalized_title = normalize_title(title)
                    if normalized_title and is_near_duplicate_title(normalized_title, seen_titles):
                        continue

                    seen_links.add(link)
                    if normalized_title:
                        seen_titles.add(normalized_title)

                    articles.append({
                        "title": title,
                        "link": link,
                        "published": published.isoformat(),
                        "source": source["name"],
                        "type": source["type"],
                        "language": source.get("language", "en"),
                        "summary": summary,
                        "image": image_url,
                        "other_news": broad_match and not marine_match
                    })
                except Exception as e:
                    logger.debug(f"Error processing entry: {str(e)}")
                    continue
        except Exception as e:
            logger.error(f"Error fetching from {source['name']}: {str(e)}")
            continue

    logger.info(f"Fetched {len(articles)} articles")
    return articles


def categorize_articles(articles):
    """Categorize articles into different types - more inclusive"""
    categories = {"Politics & Policy": [], "Research & Science": [], "General News": [], "To Watch / Read / Listen": [], "Other News": []}

    for art in articles:
        if art.get("other_news"):
            categories["Other News"].append(art)
            continue

        title_lower = art["title"].lower()
        summary_lower = art["summary"].lower()
        text = title_lower + " " + summary_lower
        source = art["source"].lower()

        if MULTIMEDIA_PATTERN.search(title_lower):
            categories["To Watch / Read / Listen"].append(art)
        elif art["type"] == "research" or RESEARCH_SOURCE_PATTERN.search(source):
            categories["Research & Science"].append(art)
        elif POLICY_PATTERN.search(text):
            categories["Politics & Policy"].append(art)
        else:
            categories["General News"].append(art)

    return categories


def simple_clustering(articles):
    """Simple keyword-based clustering aligned with marine science themes"""
    if not articles:
        return {}

    clusters = {theme: [] for theme in THEME_KEYWORDS}
    clusters["Other Marine News"] = []

    for article in articles:
        title = article["title"].lower()
        summary = article["summary"].lower()
        text = title + " " + summary

        matched = False
        for theme, pattern in THEME_PATTERNS.items():
            if pattern.search(text):
                clusters[theme].append(article)
                matched = True
                break

        if not matched:
            clusters["Other Marine News"].append(article)

    return {k: v for k, v in clusters.items() if v}


def build_news_payload():
    """Runs the full pipeline and returns the category -> theme -> articles dict"""
    articles = fetch_articles()
    categories = categorize_articles(articles)

    for cat in categories:
        if categories[cat]:
            categories[cat] = simple_clustering(categories[cat])
        else:
            categories[cat] = {}

    return categories


def main():
    payload = build_news_payload()
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": payload
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

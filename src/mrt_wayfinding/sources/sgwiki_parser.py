"""Conservative extraction from rendered SgWiki station pages, without an LLM.

This parser intentionally encodes the observed table and annotation formats.
Unknown markup, columns, types and natural-language annotations raise a source
contract error instead of degrading silently.
"""
import json
import re
from urllib.parse import quote, unquote, urlencode, urlsplit

from bs4 import BeautifulSoup

from ..contracts import ContractError


class SgWikiSourceError(ContractError):
    """Raised when a saved SgWiki response violates the parser contract."""


def require(condition, message):
    if not condition:
        raise SgWikiSourceError(message)


def fields(value, expected, context):
    require(type(value) is dict, f"{context}: expected object")
    actual = set(value)
    expected = set(expected)
    require(
        actual == expected,
        f"{context}: unexpected fields; missing={sorted(expected-actual)}, "
        f"extra={sorted(actual-expected)}",
    )


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise SgWikiSourceError(f"Non-finite JSON number: {value}")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SgWikiSourceError(f"SgWiki response is not valid UTF-8 JSON: {error}") from error


LABEL = r"(?:[A-Z][0-9]{0,2}|[0-9]{1,2})"


def station_key(name):
    require(type(name) is str and bool(name.strip()), "Station name must be nonempty text")
    normalized = " ".join(name.upper().split())
    return re.sub(r" (?:MRT/LRT|MRT|LRT) STATION$", "", normalized)


def rail_scopes(codes):
    return sorted({
        "LRT" if code in ("STC", "PTC") or code.startswith(("BP", "SW", "SE", "PW", "PE"))
        else "MRT"
        for code in codes
    })

WIKIS = {"sgwiki": "https://sgwiki.miraheze.org"}
EXIT_HEADINGS = {"exits", "entrances/exits", "entrances and exits", "station exits"}
HEADERS = {"exit", "exits", "location", "location/nearby", "nearby point of interest",
           "nearby points of interest", "disabled-access", "disabled access", "wheelchair access",
           "line", "station", ""}
SCOPE_NAMES = {"MRT": "MRT", "LRT": "LRT", "NSL": "MRT", "EWL": "MRT", "NEL": "MRT",
               "CCL": "MRT", "DTL": "MRT", "TEL": "MRT", "BPLRT": "LRT", "SKLRT": "LRT", "PGLRT": "LRT",
               "BP": "LRT"}


def clean(text):
    return " ".join(text.replace("\xa0", " ").split())


def api_url(source, title):
    require(source == "sgwiki", f"Unsupported wiki source {source!r}")
    path = "/w/api.php"
    return WIKIS[source] + path + "?" + urlencode({"action": "parse", "page": title,
        "prop": "text|revid", "format": "json", "formatversion": 2, "redirects": 1})


def page_url(source, title):
    require(source == "sgwiki", f"Unsupported wiki source {source!r}")
    return WIKIS[source] + "/wiki/" + quote(title.replace(" ", "_"), safe="/")


def validate_title(title, station):
    require(type(title) is str, "Wiki title must be text")
    match = re.fullmatch(r"(.+?)(?: (?:MRT/LRT|MRT|LRT))? [Ss]tation", title.replace("_", " "))
    require(match is not None and station_key(match[1]) == station_key(station["name"]),
            f"Wiki returned an unexpected station title: {title!r}")


def table_grid(table, *, sgwiki_scope_overrun=False, reviews=None):
    """Expand spans while preserving cell identity, including shared scope cells."""
    require(not table.find("table"), "Nested exit tables need an explicit extraction rule")
    grid, pending = [], {}
    for row in table.find_all("tr"):
        cells, current = row.find_all(["th", "td"], recursive=False), {}
        for col, (cell, remaining) in list(pending.items()):
            current[col] = cell
            if remaining == 1:
                del pending[col]
            else:
                pending[col] = (cell, remaining - 1)
        col = 0
        for cell in cells:
            while col in current:
                col += 1
            spans = []
            for attr in ("rowspan", "colspan"):
                value = cell.get(attr, "1")
                require(type(value) is str and re.fullmatch(r"[1-9][0-9]?", value), f"Unsupported {attr}")
                spans.append(int(value))
            height, width = spans
            require(width <= 12, "Excessive exit table width")
            for target in range(col, col + width):
                require(target not in current, "Overlapping table spans")
                current[target] = cell
                if height > 1:
                    pending[target] = (cell, height - 1)
            col += width
        require(current and set(current) == set(range(len(current))), "Empty/gapped exit table row")
        grid.append([current[c] for c in range(len(current))])
    if pending and sgwiki_scope_overrun:
        # The current Dhoby Ghaut and Orchard pages overstate only the
        # line-code cell's trailing rowspan. Labels stay in their own column.
        only_scope = (set(pending) == {0} and pending[0][0].find(class_="stncode")
                      and pending[0][1] <= 2)
        if only_scope:
            require(reviews is not None, "SgWiki scope overrun needs a review sink")
            reviews.append({"reason": "line_scope_rowspan_overruns_table",
                            "scope_verbatim": clean(pending[0][0].get_text(" ", strip=True)),
                            "extra_rows": pending[0][1]})
            pending = {}
    require(not pending, "Rowspan extends past the exit table")
    require(len(grid) >= 2 and all(len(row) == len(grid[0]) for row in grid), "Ragged/empty exit table")
    return grid


def scopes_from_text(text):
    text = clean(text).upper()
    match = re.fullmatch(r"(MRT|LRT)(?: (?:STATION|EXITS?|ENTRANCES/EXITS))?", text)
    if match:
        return [match[1]]
    tokens = re.split(r"[\s/&,+]+", text)
    if tokens and all(token in SCOPE_NAMES for token in tokens):
        return sorted({SCOPE_NAMES[token] for token in tokens})
    return None


_CROSSED_WHEELCHAIR_OVERLAY_STYLE = (
    "top:0;left:0;width:100%;height:100%;"
    "background:linear-gradient(to top right,#0000 calc(50% - 1px),"
    "#f00 calc(50% - 1px),#f00 calc(50% + 1px),#0000 calc(50% + 1px))"
)


def wheelchair_access_from_cell(cell):
    """Classify only SgWiki's exact crossed-wheelchair symbol."""

    images = cell.find_all("img")
    if not images:
        return "not_classified", False
    if len(images) != 1:
        return "not_classified", True

    image = images[0]
    source = urlsplit(image.get("src", ""))
    asset = unquote(source.path).rsplit("/", 1)[-1]
    wrapper = image.find_parent("div", class_="pos-rel")
    overlays = [] if wrapper is None else [
        child for child in wrapper.find_all("div", recursive=False)
        if set(child.get("class", [])) == {"pos-abs"}
    ]
    crossed = (
        asset == "Icon-WC.svg"
        and source.hostname == "static.wikitide.net"
        and not source.query
        and not source.fragment
        and wrapper is not None
        and set(wrapper.get("class", [])) == {"pos-rel", "d-inline-block"}
        and len(overlays) == 1
        and re.sub(r"\s+", "", overlays[0].get("style", ""))
        == re.sub(r"\s+", "", _CROSSED_WHEELCHAIR_OVERLAY_STYLE)
    )
    return ("not_accessible", False) if crossed else ("not_classified", True)


def cell_labels(cell, *, source=None):
    text = clean(cell.get_text(" ", strip=True))
    if source == "sgwiki":
        clusters = cell.select("div.d-flex.align-items-center.tx-center")
        if clusters:
            require(len(clusters) == 1, "Multiple SgWiki exit-badge clusters in one cell")
            badge_nodes = clusters[0].find_all("div", class_="fw-bold", recursive=False)
            require(badge_nodes, "SgWiki exit-badge cluster has no labels")
            labels = [clean(node.get_text(" ", strip=True)) for node in badge_nodes]
            require(all(re.fullmatch(LABEL, label) for label in labels),
                    f"Invalid SgWiki exit badge: {labels!r}")
            require(len(labels) == len(set(labels)), "Repeated label inside SgWiki badge cluster")
            status_nodes = cell.find_all("small")
            require(len(status_nodes) <= 1, "Multiple SgWiki annotations in one exit cell")
            exit_status = None
            if status_nodes:
                node = status_nodes[0]
                expected_classes = {"d-block", "mx-auto", "fw-normal", "tx-center"}
                require(set(node.get("class", [])) == expected_classes,
                        "Unknown SgWiki exit annotation markup")
                status_verbatim = clean(node.get_text(" ", strip=True))
                match = re.fullmatch(r"\(Closed – use Exit (" + LABEL + r"(?:/" + LABEL + r")*)\)",
                                     status_verbatim)
                require(match is not None,
                        f"Unknown SgWiki exit annotation: {status_verbatim!r}")
                alternatives = match[1].split("/")
                require(len(alternatives) == len(set(alternatives))
                        and not set(alternatives).intersection(labels),
                        "Invalid SgWiki alternative exits")
                exit_status = {"status": "closed", "alternative_exits": alternatives,
                               "status_verbatim": status_verbatim,
                               "status_markup": "small.d-block.mx-auto.fw-normal.tx-center"}
            # Everything after the badge cluster is a destination or symbol,
            # not another exit label. Only the exact crossed-wheelchair symbol
            # is classified; any other image remains reviewable.
            wheelchair_access, has_uninterpreted_icon = wheelchair_access_from_cell(cell)
            remainder = BeautifulSoup(str(cell), "html.parser")
            for node in remainder.select("div.d-flex.align-items-center.tx-center > div.fw-bold"):
                node.decompose()
            for node in remainder.find_all("small"):
                node.decompose()
            destination = clean(remainder.get_text(" ", strip=True))
            return labels, "", text, {"destination_verbatim": destination,
                                        "label_markup": "sgwiki_exit_badges",
                                        "wheelchair_access": wheelchair_access,
                                        "has_uninterpreted_icon": has_uninterpreted_icon,
                                        "destination_missing": not destination,
                                        **({"exit_status": exit_status} if exit_status else {})}
    plain = re.sub(r"^Exit\s+", "", text)
    if re.fullmatch(LABEL + r"(?:(?:\s*[,/&]\s*|\s+)" + LABEL + r")*", plain):
        labels = re.split(r"\s*[,/&]\s*|\s+", plain)
        require(len(labels) == len(set(labels)), "Repeated label inside wiki exit cell")
        return labels, "", text, {}
    # Labels are extracted ONLY at the start of the dedicated exit column.
    # A closure note containing A/C/E can never contribute those letters.
    match = re.match(r"^(?:Exit\s+)?(" + LABEL + r"(?:\s*[,/&]\s*" + LABEL + r")*)(?=$|[\s(:\-–—])", text)
    require(match is not None, f"Unrecognized exit-label cell: {text!r}")
    labels = re.split(r"\s*[,/&]\s*", match[1])
    require(len(labels) == len(set(labels)), "Repeated label inside wiki exit cell")
    note = text[match.end():].strip()
    require(not re.match(r"^" + LABEL + r"(?:\s|$)", note), "Additional unparsed exit label in annotated cell")
    return labels, note, text, {}


def section_table(soup, title):
    headings = [h for h in soup.find_all(["h2", "h3"])
                if clean(h.get_text(" ", strip=True)).lower() == title]
    require(len(headings) <= 1, f"Repeated SgWiki {title} section")
    if not headings:
        return None
    heading = headings[0]
    tables = [table for table in soup.find_all("table")
              if not table.find_parent("table") and table.find_previous("h2") == heading]
    require(tables and (title == "layout" or len(tables) == 1),
            f"SgWiki {title}: expected one top-level table")
    return tables[0]


def direct_rows(table):
    return [row for row in table.find_all("tr") if row.find_parent("table") is table]


def layout_supplementary_table(table, number):
    """Classify an observed top-level Layout table without parsing its topology."""

    descendants = table.find_all()
    tags = {node.name for node in descendants}
    rows = direct_rows(table)
    cells = [
        cell
        for row in rows
        for cell in row.find_all(["th", "td"], recursive=False)
    ]
    empty_formatting_table = (
        not table.attrs
        and tags == {"tbody", "tr", "td"}
        and len(rows) == 1
        and len(cells) == 1
        and not cells[0].attrs
        and not cells[0].find()
        and not clean(cells[0].get_text(" ", strip=True))
    )
    if empty_formatting_table:
        return {"table": number, "kind": "empty_formatting_table"}

    badges = table.select(".stncode")
    allowed_tags = {"tbody", "tr", "td", "div", "a", "span"}
    badge_classes = {"both", "left", "right"}
    is_service_diagram = (
        len(badges) >= 2
        and tags <= allowed_tags
        and set(table.attrs) <= {"class"}
        and set(table.get("class", [])) <= {"tx-center"}
        and all(
            set(badge.get("class", [])) == {"stncode", position}
            for badge in badges
            for position in badge_classes
            if position in badge.get("class", [])
        )
        and all(
            len(set(badge.get("class", [])) & badge_classes) == 1
            and re.fullmatch(r"(?:[A-Z]{1,3}[0-9]+|STC|PTC|CG)",
                             clean(badge.get_text(" ", strip=True)))
            for badge in badges
        )
        and bool(table.select("a.mw-selflink.selflink"))
        and all(
            (
                set(anchor.get("class", [])) == {"mw-selflink", "selflink"}
                and not anchor.get("href")
            )
            or (
                not anchor.get("class")
                and re.fullmatch(r"/wiki/[A-Za-z0-9_%()'.,-]+_(?:station|depot)",
                                 anchor.get("href", ""))
            )
            for anchor in table.find_all("a")
        )
    )
    require(
        is_service_diagram,
        f"SgWiki Layout supplementary table {number}: unknown structure",
    )
    return {"table": number, "kind": "rail_service_diagram"}


def bus_connections(soup):
    table = section_table(soup, "bus connections")
    if table is None:
        return {"status": "unavailable", "labels": [], "rows": [],
                "unlinked_rows": [], "reviews": []}
    rows = direct_rows(table)
    require(len(rows) >= 2, "SgWiki bus connections table is empty")
    header = [clean(c.get_text(" ", strip=True)) for c in rows[0].find_all(["th", "td"], recursive=False)]
    require(header == ["Bus Stop", "Exit", "Bus Service"], f"Unknown SgWiki bus columns: {header!r}")
    entries, unlinked_rows, reviews, no_service = [], [], [], False
    for number, row in enumerate(rows[1:], 2):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) == 1 and cells[0].get("colspan") == "3" and clean(cells[0].get_text(" ", strip=True)) == "N/A":
            require(number == 2 and len(rows) == 2, "SgWiki bus N/A mixed with stop rows")
            no_service = True
            break
        require(len(cells) == 3 and all(c.name == "td" for c in cells),
                f"SgWiki bus row {number}: unexpected cell layout")
        stop = clean(cells[0].get_text(" ", strip=True))
        match = re.search(r"\b[0-9]{5}\b", stop)
        require(match is not None and clean(cells[2].get_text(" ", strip=True)),
                f"SgWiki bus row {number}: missing stop code or service")
        raw_labels = clean(cells[1].get_text(" ", strip=True))
        if not raw_labels:
            reviews.append({"reason": "bus_stop_without_exit_link", "row": number, "stop_verbatim": stop})
            unlinked_rows.append({
                "row": number,
                "bus_stop_code": match[0],
                "stop_verbatim": stop,
                "service_verbatim": clean(cells[2].get_text(" ", strip=True)),
            })
            continue
        labels = raw_labels.split()
        require(all(re.fullmatch(LABEL, label) for label in labels)
                and len(labels) == len(set(labels)),
                f"SgWiki bus row {number}: unexpected exit labels {raw_labels!r}")
        entries.append({"row": number, "bus_stop_code": match[0], "stop_verbatim": stop,
                        "labels": labels, "service_verbatim": clean(cells[2].get_text(" ", strip=True))})
    return {"status": "no_service" if no_service else "partial_positive_evidence",
            "labels": sorted({label for entry in entries for label in entry["labels"]}),
            "rows": entries, "unlinked_rows": unlinked_rows, "reviews": reviews}


_DAYS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]


def exit_operating_hours(prose, entries):
    """Parse only the exact daily-hours sentence currently observed on SgWiki."""

    remaining, hours = [], []
    pattern = re.compile(
        r"Notes: Exit (" + LABEL +
        r") is open from ([0-2][0-9]:[0-5][0-9]) - ([0-2][0-9]:[0-5][0-9]) daily\."
    )
    for text in prose:
        match = pattern.fullmatch(text)
        if match is None:
            remaining.append(text)
            continue
        label, opens, closes = match.groups()
        require(int(opens[:2]) < 24 and int(closes[:2]) < 24,
                f"Invalid SgWiki exit operating hour: {text!r}")
        require(opens < closes, f"Overnight SgWiki exit hours need an explicit rule: {text!r}")
        matching_scopes = {
            tuple(entry["scopes"] or [])
            for entry in entries
            if label in entry["labels"]
        }
        require(len(matching_scopes) == 1,
                f"SgWiki exit operating hours do not identify one exit scope: {text!r}")
        hours.append({
            "labels": [label],
            "scopes": list(next(iter(matching_scopes))),
            "days": list(_DAYS),
            "opens": opens,
            "closes": closes,
            "timezone": "Asia/Singapore",
            "source_verbatim": text,
            "grammar": "daily_open_interval",
        })
    return hours, remaining


def layout_inventory(soup, station):
    table = section_table(soup, "layout")
    require(table is not None, "SgWiki station has no Layout table")
    floors, reviews, floor, remaining = [], [], None, 0
    top_level_tables = [item for item in soup.find_all("table")
                        if not item.find_parent("table")
                        and item.find_previous("h2") == table.find_previous("h2")]
    supplementary_tables = [
        layout_supplementary_table(item, number)
        for number, item in enumerate(top_level_tables[1:], 2)
    ]
    for number, row in enumerate(direct_rows(table), 1):
        cells = row.find_all(["th", "td"], recursive=False)
        require(1 <= len(cells) <= 3, f"SgWiki Layout row {number}: unexpected columns")
        if cells[0].name == "th":
            require(remaining == 0, f"SgWiki Layout row {number}: prior floor rowspan overlaps")
            floor = clean(cells[0].get_text(" ", strip=True))
            require(re.fullmatch(r"(?:L[0-9]+|B[0-9]+|M|P|U[0-9]+)", floor),
                    f"SgWiki Layout row {number}: unknown level {floor!r}")
            span = cells[0].get("rowspan", "1")
            require(re.fullmatch(r"[1-9][0-9]?", span), "SgWiki Layout floor rowspan invalid")
            remaining = int(span)
            details = cells[1:]
        else:
            require(remaining > 0, f"SgWiki Layout row {number}: missing floor label")
            details = cells
        remaining -= 1
        require(floor is not None, "SgWiki Layout starts without a floor")
        plain = []
        platforms = []
        for cell in details:
            plain.extend(clean(str(node)) for node in cell.find_all(string=True)
                         if node.find_parent("table") is table and clean(str(node)))
            for caption in cell.find_all("caption"):
                caption_text = clean(caption.get_text(" ", strip=True))
                require(re.fullmatch(r"(?:[A-Z]+(?: [A-Za-z]+)? )?Platform|"
                        r"(?:Upper|Lower) Platform", caption_text),
                        f"SgWiki Layout row {number}: unexpected platform caption {caption_text!r}")
                platform_table = caption.find_parent("table")
                badges = []
                for div in platform_table.find_all("div"):
                    bold = div.find_all("b", recursive=False)
                    small = div.find_all("small", recursive=False)
                    if len(bold) == len(small) == 1 and clean(small[0].get_text(" ", strip=True)) == "Platform":
                        label = clean(bold[0].get_text(" ", strip=True))
                        require(re.fullmatch(LABEL + r"(?:/" + LABEL + r")?", label),
                                f"SgWiki Layout row {number}: invalid platform label {label!r}")
                        if "/" in label:
                            reviews.append({"reason": "combined_platform_label", "row": number,
                                            "floor": floor, "caption": caption_text, "label": label})
                        heading = clean(div.find_parent("tr").get_text(" ", strip=True)).split("Service Map", 1)[0].strip()
                        badges.append({"label": label, "heading_verbatim": heading})
                require(badges, f"SgWiki Layout row {number}: caption without platform badge")
                require(len({badge["label"] for badge in badges}) == len(badges),
                        f"SgWiki Layout row {number}: duplicate platform in one caption")
                platforms.extend({"line_or_area": caption_text, "line_or_area_source": "caption", **badge}
                                 for badge in badges)
            # Five observed EWL pages place each platform card directly in the
            # floor row, without an enclosing line caption. Cards nested below
            # a caption have already been handled above.
            for card in cell.select("table.mw-collapsible"):
                if card.find("caption") or any(parent.find("caption", recursive=False) for parent in card.parents
                       if parent.name == "table" and parent is not table):
                    continue
                require(len(station["codes"]) == 1 and re.fullmatch(r"EW[0-9]+", station["codes"][0]),
                        "Captionless platform card needs an explicit line rule")
                badges = []
                for div in card.find_all("div"):
                    bold = div.find_all("b", recursive=False)
                    small = div.find_all("small", recursive=False)
                    if len(bold) == len(small) == 1 and clean(small[0].get_text(" ", strip=True)) == "Platform":
                        label = clean(bold[0].get_text(" ", strip=True))
                        require(re.fullmatch(LABEL, label),
                                f"SgWiki Layout row {number}: invalid captionless platform {label!r}")
                        heading = clean(div.find_parent("tr").get_text(" ", strip=True)).split("Service Map", 1)[0].strip()
                        badges.append({"line_or_area": "EWL Platform", "line_or_area_source": "station_code",
                                       "label": label, "heading_verbatim": heading})
                require(len(badges) == 1, f"SgWiki Layout row {number}: captionless platform card needs one badge")
                platforms.extend(badges)
        area = clean(" ".join(plain))
        floors.append({"row": number, "floor": floor, "area_verbatim": area,
                       "platforms": platforms})
    if remaining:
        require(remaining <= 2, "SgWiki Layout floor rowspan extends beyond table")
        reviews.append({"reason": "layout_floor_rowspan_overruns_table",
                        "floor": floor, "extra_rows": remaining})
    require(floors, "SgWiki Layout table has no levels")
    return {
        "floors": floors,
        "supplementary_tables": supplementary_tables,
        "reviews": reviews,
    }


def html_inventory(html, station, *, source=None):
    soup = BeautifulSoup(html, "html.parser")
    for edit in soup.select(".mw-editsection"):
        edit.decompose()
    require(not soup.select(".error, .mw-ext-cite-error"), "Wiki rendered an error; inspect saved HTML")
    sgwiki = source == "sgwiki"
    bus = bus_connections(soup) if sgwiki else None
    layout = layout_inventory(soup, station) if sgwiki else None
    entries, reviews, prose, sections = [], [], [], []
    active, level, scope = False, None, None
    default_scope = rail_scopes(station["codes"])
    default_scope = default_scope if len(default_scope) == 1 else None
    tables = 0
    for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table"]):
        if element.find_parent("table") or element.find_parent(["ul", "ol"]):
            continue
        text = clean(element.get_text(" ", strip=True))
        if re.fullmatch(r"h[1-6]", element.name):
            heading_level = int(element.name[1])
            normalized = text.lower().replace(" / ", "/")
            scoped_heading = re.fullmatch(r"(mrt|lrt) (?:exits|entrances/exits)", normalized)
            require(normalized in EXIT_HEADINGS or scoped_heading or not re.search(r"\b(?:exits?|entrances?)\b", normalized),
                    f"Unrecognized exit-related heading: {text!r}")
            if normalized in EXIT_HEADINGS or scoped_heading:
                if not active or heading_level <= level:
                    level = heading_level
                active = True
                scope = [scoped_heading[1].upper()] if scoped_heading else default_scope
                sections.append(text)
            elif active and (heading_level <= level or normalized == "first/last train timing"):
                active = False
            elif active:
                scope = scopes_from_text(text)
                require(scope is not None, f"Unknown heading inside wiki exit section: {text!r}")
            continue
        if not active:
            continue
        if element.name != "table":
            if text:
                found_scope = scopes_from_text(text)
                if found_scope:
                    scope = found_scope
                else:
                    prose.append(text)
            continue
        tables += 1
        grid = table_grid(element, sgwiki_scope_overrun=sgwiki, reviews=reviews)
        headers = [clean(c.get_text(" ", strip=True)).lower() for c in grid[0]]
        require(all(x in HEADERS for x in headers), f"Unknown wiki exit columns: {headers!r}")
        exit_columns = [i for i, x in enumerate(headers) if x in ("exit", "exits")]
        require(exit_columns and len({id(grid[0][i]) for i in exit_columns}) == 1,
                "Need exactly one wiki exit column (possibly spanning scope and label)")
        label_col = max(exit_columns)
        require(label_col <= 1, "Unsupported columns preceding wiki exit labels")
        seen_cells = set()
        for row_number, row in enumerate(grid[1:], 2):
            label_cell = row[label_col]
            if id(label_cell) in seen_cells:
                reviews.append({"reason": "exit_label_rowspan", "table": tables, "row": row_number})
                continue
            seen_cells.add(id(label_cell))
            labels, note, verbatim, detail = cell_labels(label_cell, source=source)
            row_scope = scope
            if label_col == 1:
                require(row[0] is not label_cell, "Exit row merges scope and label; explicit rule required")
                row_scope = scopes_from_text(row[0].get_text(" ", strip=True))
                require(row_scope is not None, "Unknown line/scope in wiki exit table")
            require(row_scope is None or set(row_scope) <= set(rail_scopes(station["codes"])),
                    "Wiki exit scope disagrees with station inventory")
            entry = {"labels": labels, "scopes": row_scope, "label_cell_verbatim": verbatim,
                     "label_note_verbatim": note, "row_verbatim": [clean(c.get_text(" ", strip=True)) for c in row],
                     "table": tables, "row": row_number, **detail}
            entries.append(entry)
            if note:
                reviews.append({"reason": "exit_label_note_not_interpreted", "table": tables,
                                "row": row_number, "text": verbatim})
            if row_scope is None:
                reviews.append({"reason": "unspecified_exit_scope", "table": tables,
                                "row": row_number, "text": verbatim})
            if detail.get("has_uninterpreted_icon"):
                reviews.append({"reason": "exit_cell_icon_not_interpreted", "table": tables,
                                "row": row_number, "text": verbatim})
            if detail.get("exit_status"):
                reviews.append({"reason": "exit_status_requires_review", "table": tables,
                                "row": row_number, "labels": labels, **detail["exit_status"]})
    if sgwiki and sections and not tables and not entries and bus["labels"]:
        reviews.append({"reason": "exit_table_missing_bus_fallback", "bus_labels": bus["labels"]})
        reviews.extend({"section": "layout", **item} for item in layout["reviews"])
        reviews.extend({"section": "bus_connections", **item} for item in bus["reviews"])
        return {"status": "partial", "evidence_kind": "bus_connections_only",
                "labels": bus["labels"], "entries": [], "sections": sections,
                "prose_verbatim": prose, "exit_statuses": [], "exit_operating_hours": [],
                "reviews": reviews,
                "bus_connections": bus, "layout": layout, "requires_human_review": True}
    require(sections and tables and entries, "No supported exit table; reference unavailable, not zero exits")
    seen = set()
    for entry in entries:
        for mode in entry["scopes"] or ["unspecified"]:
            for label in entry["labels"]:
                identity = (mode, label)
                if identity in seen and sgwiki:
                    reviews.append({"reason": "repeated_exit_identity_in_source_table", "identity": identity,
                                    "table": entry["table"], "row": entry["row"]})
                    continue
                require(identity not in seen, f"Repeated wiki exit identity {identity}; review table layout")
                seen.add(identity)
    operating_hours, prose = exit_operating_hours(prose, entries) if sgwiki else ([], prose)
    if prose:
        reviews.append({"reason": "exit_section_prose_not_interpreted", "text": prose})
    if sgwiki:
        extra_bus = sorted(set(bus["labels"]) - {x for e in entries for x in e["labels"]})
        if extra_bus:
            reviews.append({"reason": "bus_exit_not_in_exit_table", "labels": extra_bus})
        reviews.extend({"section": "layout", **item} for item in layout["reviews"])
        reviews.extend({"section": "bus_connections", **item} for item in bus["reviews"])
    return {"status": "complete", "labels": sorted({x for e in entries for x in e["labels"]}),
            "entries": entries, "sections": sections, "prose_verbatim": prose, "reviews": reviews,
            "exit_statuses": [{"labels": entry["labels"], "scopes": entry["scopes"],
                               "destination_verbatim": entry["destination_verbatim"],
                               **entry["exit_status"]}
                              for entry in entries if entry.get("exit_status")],
            "exit_operating_hours": operating_hours,
            "requires_human_review": True,
            **({"bus_connections": bus, "layout": layout} if sgwiki else {})}


def parse_response(raw, station, *, source=None):
    data = loads(raw)
    # API errors and warnings deliberately cannot become an empty inventory.
    fields(data, {"parse"}, "Wiki API envelope (errors/warnings need review)")
    parsed = data["parse"]
    require(type(parsed) is dict and {"title", "pageid", "revid", "text"} <= parsed.keys()
            and set(parsed) <= {"title", "pageid", "revid", "text", "redirects"}, "Unexpected Wiki parse fields")
    require(all(type(parsed[k]) is int and parsed[k] > 0 for k in ("pageid", "revid"))
            and type(parsed["text"]) is str and parsed["text"], "Invalid Wiki parse types")
    redirects = parsed.get("redirects", [])
    require(type(redirects) is list, "Invalid wiki redirects")
    for redirect in redirects:
        fields(redirect, {"from", "to"}, "Wiki redirect")
        require(all(type(x) is str for x in redirect.values()), "Invalid wiki redirect types")
        validate_title(redirect["to"], station)
    validate_title(parsed["title"], station)
    result = html_inventory(parsed["text"], station, source=source)
    result.update(title=parsed["title"], page_id=parsed["pageid"], revision_id=parsed["revid"], redirects=redirects)
    return result, parsed["text"]

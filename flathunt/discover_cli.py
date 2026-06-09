import argparse

from flathunt.discover import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m flathunt.discover",
        description=(
            "Scrape SpareRoom for London flats matching your criteria, "
            "rate photos with Gemini Vision, and append matches to the Google Sheet."
        ),
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=5,
        metavar="N",
        help="Number of SpareRoom search result pages to scrape (default 5, ~50 listings).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matches without writing anything to the sheet.",
    )
    args = parser.parse_args()
    run(pages=args.pages, dry_run=args.dry_run)

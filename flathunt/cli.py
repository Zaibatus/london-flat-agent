import argparse

from flathunt.enrich import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m flathunt.enrich",
        description=(
            "Enrich flat listing rows in Google Sheets using Gemini. "
            "Reads URLs from column A and fills empty B–H cells. "
            "Columns I–L are never touched."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing cell values in B–H (I–L are always protected).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Process at most N rows.",
    )
    parser.add_argument(
        "--row",
        type=int,
        metavar="R",
        help=(
            "Process only sheet row R (1-based; row 1 is the header, "
            "so the first data row is 2)."
        ),
    )
    args = parser.parse_args()
    run(force=args.force, limit=args.limit, row=args.row)

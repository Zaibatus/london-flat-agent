import argparse

from flathunt.verify import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m flathunt.verify",
        description=(
            "Re-fetch every listing in the sheet, re-extract key fields and re-rate photos, "
            "then report any discrepancies or criteria violations."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Check only the first N rows (useful for a quick test).",
    )
    parser.add_argument(
        "--row",
        type=int,
        metavar="N",
        help="Check a single sheet row by its 1-based row number.",
    )
    args = parser.parse_args()
    run(limit=args.limit, row=args.row)

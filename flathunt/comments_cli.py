import argparse

from flathunt.comments import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m flathunt.comments",
        description=(
            "Check unresolved @agent comments in the Google Sheet, "
            "classify intent with Gemini, and post replies."
        ),
    )
    parser.parse_args()
    run()

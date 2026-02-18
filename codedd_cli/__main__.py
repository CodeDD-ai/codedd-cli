"""Allow running via ``python -m codedd_cli``."""

import sys

from codedd_cli.api.exceptions import CodeDDConnectionError
from codedd_cli.cli import app
from codedd_cli.utils.display import print_error


def main() -> None:
    try:
        app()
    except CodeDDConnectionError as e:
        print_error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()

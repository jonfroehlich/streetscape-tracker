#!/usr/bin/env python3
"""
Multi-city runner for Streetscape Metadata Tracker.

This script provides a wrapper around the Streetscape Metadata Tracker to process multiple cities.
Each line in the input file represents a complete command line style configuration for a city.

Example cities.txt format:
    Seattle, WA --width 2000 --height 2000 --step 25
    Portland, OR --width 1500 --height 1500
    Vancouver, BC
"""

import argparse
import logging
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from streetscape_metadata_tracker.paths import get_default_data_dir, get_project_root


def parse_city_line(line: str) -> list[str] | None:
    """
    Parse a line from the cities file into command line arguments.

    Each line should be formatted exactly as you would type it on the command line.
    The city name comes first, followed by any optional parameters.

    Args:
        line: A line from the cities file

    Returns:
        List[str] if valid line, None if comment or empty

    Examples:
        >>> parse_city_line("Seattle, WA --width 2000 --height 2000 --step 25")
        ["Seattle, WA", "--width", "2000", "--height", "2000", "--step", "25"]
        >>> parse_city_line("Grand Marais, MN")
        ["Grand Marais, MN"]
        >>> parse_city_line("# This is a comment")
        None
    """
    # Skip comments and empty lines
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Use shlex to properly handle quoted strings and spaces
    return shlex.split(line)


def load_cities(file_path: str) -> list[list[str]]:
    """
    Load city configurations from file.

    Args:
        file_path: Path to file containing city configurations

    Returns:
        List[List[str]]: List of command line argument lists for each city

    Raises:
        FileNotFoundError: If cities file doesn't exist
    """
    cities = []

    with open(file_path) as f:
        for line_num, line in enumerate(f, 1):
            try:
                args = parse_city_line(line)
                if args:
                    cities.append(args)
            except ValueError as e:
                logging.warning(f"Invalid configuration at line {line_num}: {line.strip()}")
                logging.warning(f"Error: {str(e)}")
                continue

    return cities


def parse_args() -> argparse.Namespace:
    """
    Parse and validate command line arguments.

    Returns:
        argparse.Namespace: Parsed command line arguments
    """
    parser = argparse.ArgumentParser(
        description="Run Streetscape Tracker for multiple cities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python run_cities.py cities.txt
  python run_cities.py cities.txt --batch-size 200 --connection-limit 100
  python run_cities.py cities.txt --download-dir ./data --log-level DEBUG""",
    )

    parser.add_argument(
        "cities_file",
        type=str,
        help="Path to file containing city configurations (e.g., cities.txt)",
    )

    # Global execution parameters
    exec_group = parser.add_argument_group("Execution Parameters")
    exec_group.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of requests to prepare and queue at once",
    )
    exec_group.add_argument(
        "--connection-limit", type=int, default=50, help="Maximum number of concurrent connections"
    )
    exec_group.add_argument(
        "--no-visual", action="store_true", help="Skip generating visualizations"
    )
    exec_group.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Set logging level",
    )
    exec_group.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing remaining cities if one fails",
    )
    exec_group.add_argument(
        "--download-dir",
        type=str,
        help="Dir to save downloaded data (defaults to ./data)",
        default=get_default_data_dir(),
    )

    args = parser.parse_args()

    # Clamp, never refuse, exactly as streetscape_tracker.py's own parser does
    # (issue #359): the GSV engine gathers at most batch_size requests per batch,
    # so sockets past the batch never get work. Clamped HERE too, before the
    # value is forwarded, so a batch warns once rather than once per city.
    if args.connection_limit > args.batch_size:
        print(
            f"warning: --connection-limit {args.connection_limit} exceeds --batch-size "
            f"{args.batch_size}; at most batch-size requests are ever in flight, so the "
            f"effective connection limit is {args.batch_size}. Raise --batch-size to use "
            f"more sockets.",
            file=sys.stderr,
        )
        args.connection_limit = args.batch_size

    return args


def setup_logging(args: argparse.Namespace) -> str:
    """
    Set up logging configuration.

    Args:
        args: Parsed command line arguments

    Returns:
        str: Path to the log file
    """
    # Create output directory if specified
    if args.download_dir:
        Path(args.download_dir).mkdir(parents=True, exist_ok=True)

    # Logs go to logs/, never data/ (data/ is synced to the public web server)
    log_dir = Path(get_project_root()) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"streetscape_tracker_{timestamp}.log"

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)],
    )

    return str(log_path)


def split_city_and_flags(city_args: list[str]) -> tuple[str, list[str]]:
    """
    Split one parsed cities-file line into (city_name, flag_tokens).

    Tokens before the first ``--flag`` are the (possibly unquoted) city
    name and are rejoined with single spaces — so both
    ``Seattle, WA --width 2000`` and ``"Seattle, WA" --width 2000`` yield
    the city name ``"Seattle, WA"``. Everything from the first flag on
    (including flag values) passes through unchanged.

    Args:
        city_args: Tokens from parse_city_line().

    Returns:
        (city_name, remaining_flag_and_value_tokens)
    """
    city_name_parts: list[str] = []
    other_args: list[str] = []
    for arg in city_args:
        if arg.startswith("--") or other_args:
            other_args.append(arg)
        else:
            city_name_parts.append(arg)
    return " ".join(city_name_parts), other_args


def run_streetscape_tracker(city_args: list[str], global_args: argparse.Namespace) -> bool:
    """Run the Streetscape Metadata Tracker for a specific city."""
    # Use the same interpreter (and venv) that launched this script
    cmd = [sys.executable, "streetscape_tracker.py"]

    city_name, other_args = split_city_and_flags(city_args)
    cmd.append(city_name)
    cmd.extend(other_args)

    # Add global execution args
    cmd.extend(
        [
            "--batch-size",
            str(global_args.batch_size),
            "--connection-limit",
            str(global_args.connection_limit),
            "--log-level",
            global_args.log_level,
            "--download-dir",
            str(global_args.download_dir),
        ]
    )

    if global_args.no_visual:
        cmd.append("--no-visual")

    try:
        logging.info(f"Processing {cmd[2]}")
        logging.debug(f"Command: {' '.join(cmd)}")

        # Remove capture_output=True to allow output to pass through
        subprocess.run(cmd, check=True, text=True)

        return True

    except subprocess.CalledProcessError:
        logging.error(f"Error processing {cmd[2]}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error processing {cmd[2]}: {e}")
        return False


def main() -> int:
    """
    Main entry point for the multi-city Streetscape Metadata Tracker.

    Returns:
        int: Exit code (0 for success, 1 for errors)
    """
    args = parse_args()

    # Setup logging
    log_path = setup_logging(args)
    logging.info(f"Log file: {log_path}")

    try:
        cities = load_cities(args.cities_file)
    except FileNotFoundError:
        logging.error(f"Cities file '{args.cities_file}' not found")
        return 1
    except Exception as e:
        logging.error(f"Error reading cities file: {e}")
        return 1

    if not cities:
        logging.error("No valid cities found in input file")
        return 1

    logging.info(f"Found {len(cities)} cities to process")

    successful = []
    failed = []

    for i, city_args in enumerate(cities, 1):
        logging.info(f"\nProcessing city {i}/{len(cities)}")

        if run_streetscape_tracker(city_args, args):
            successful.append(city_args[0])  # city name is first argument
        else:
            failed.append(city_args[0])
            if not args.continue_on_error:
                logging.error(
                    "\nStopping due to error. Use --continue-on-error to process remaining cities."
                )
                break

    # Print summary
    logging.info("\nProcessing complete!")
    logging.info(f"Successful: {len(successful)}/{len(cities)} cities")

    if failed:
        logging.error("\nFailed cities:")
        for city in failed:
            logging.error(f"- {city}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

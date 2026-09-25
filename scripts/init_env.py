#!/usr/bin/env python3
"""Create installation secrets without overwriting an existing environment."""
import argparse
from pathlib import Path
from env_config import new_values, write_private, encode_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent.parent / '.env')
    parser.add_argument('--admin-email', default='admin@setapi.local')
    parser.add_argument('--public-url', default='http://localhost:8055')
    args = parser.parse_args()
    values = new_values(args.admin_email, args.public_url)
    write_private(args.output, encode_env(values))
    print(f'Created {args.output}. Read SETAPI_ADMIN_PASSWORD there to sign in. Do not commit this file.')


if __name__ == '__main__':
    main()

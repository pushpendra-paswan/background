# Usage (inside the api container):
#   python scripts/generate_test_csv.py --rows 10000 --invalid-percent 10 --output uploads/test_10k.csv
# Writes a customers CSV with some deliberately bad rows and prints the counts the import should report.
import argparse
import csv
import os
import random
import sys
from datetime import date, timedelta

from faker import Faker

# Make "app" importable when this file is run as "python scripts/generate_test_csv.py"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.schemas import ALLOWED_COUNTRY_CODES

BAD_ROW_KINDS = [
    "missing name",
    "bad email format",
    "bad phone",
    "bad date of birth",
    "country not allowed",
    "duplicate email in file",
]

# Read the command line arguments
parser = argparse.ArgumentParser(description="Generate a customers CSV for testing the import")
parser.add_argument("--rows", type=int, default=10000)
parser.add_argument("--invalid-percent", type=float, default=10)
parser.add_argument("--output", default="test_customers.csv")
parser.add_argument("--seed", type=int, default=42)
args = parser.parse_args()

# The same seed always gives the same file
fake = Faker()
Faker.seed(args.seed)
rng = random.Random(args.seed)

# Pick which rows are bad, and give each one a kind (the kinds take turns)
invalid_count = round(args.rows * args.invalid_percent / 100)
bad_row_numbers = rng.sample(range(1, args.rows + 1), invalid_count)
bad_kind_by_row = {}
for position, row_number in enumerate(bad_row_numbers):
    bad_kind_by_row[row_number] = BAD_ROW_KINDS[position % len(BAD_ROW_KINDS)]

# Write the file, one row at a time
expected_counts = {kind: 0 for kind in BAD_ROW_KINDS}
valid_emails = []
today = date.today()
with open(args.output, "w", encoding="utf-8", newline="") as output_file:
    writer = csv.writer(output_file)
    writer.writerow(["name", "email", "phone", "date_of_birth", "country"])

    for row_number in range(1, args.rows + 1):
        # Start with a valid row. The email comes from the row number, so it is unique
        digits = "".join(rng.choice("0123456789") for _ in range(10))
        name = fake.name()
        email = f"user{row_number}@example.com"
        phone = rng.choice([f"+{digits}", f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}", digits])
        date_of_birth = fake.date_of_birth(minimum_age=18, maximum_age=90).isoformat()
        country = rng.choice(ALLOWED_COUNTRY_CODES)

        # A duplicate needs an earlier valid row to copy the email from. Without one, use a missing name
        kind = bad_kind_by_row.get(row_number)
        if kind == "duplicate email in file" and len(valid_emails) == 0:
            kind = "missing name"

        # Break one field, depending on the kind
        if kind == "missing name":
            name = ""
        elif kind == "bad email format":
            email = rng.choice([f"user{row_number}example.com", f"user{row_number}@", "@example.com", "user@@example.com"])
        elif kind == "bad phone":
            phone = rng.choice(["12345", "abc-def-ghij", "+1234567890123456789", "555-CALL-NOW"])
        elif kind == "bad date of birth":
            future_date = (today + timedelta(days=rng.randint(30, 3000))).isoformat()
            date_of_birth = rng.choice(["15/03/1990", "1990-13-45", "not a date", future_date])
        elif kind == "country not allowed":
            country = rng.choice(["XX", "USA", "ZZ", "India", "12"])
        elif kind == "duplicate email in file":
            email = rng.choice(valid_emails)

        # Count the row and write it
        if kind is None:
            valid_emails.append(email)
        else:
            expected_counts[kind] += 1
        writer.writerow([name, email, phone, date_of_birth, country])

# Print what the import should report
total_invalid = sum(expected_counts.values())
print(f"Wrote {args.output} (seed {args.seed})")
print("Expected counts (when the customers table has none of these emails yet):")
print(f"  total rows:   {args.rows}")
print(f"  valid rows:   {args.rows - total_invalid}")
print(f"  invalid rows: {total_invalid}")
for kind in BAD_ROW_KINDS:
    print(f"    {kind}: {expected_counts[kind]}")

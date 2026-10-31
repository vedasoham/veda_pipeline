import pandas as pd
import re
import requests
import time

input_file = "netmhc_out.txt"
output_file = "netmhcpan_output.xlsx"

rows = []
columns = None
inside_table = False
expect_data = False

with open(input_file, "r") as f:
    for line in f:
        line = line.rstrip()

        # Detect header
        if line.strip().startswith("Pos") and "Peptide" in line:
            columns = line.split()
            expect_data = False
            continue

        # Detect dashed line
        if re.match(r"-{10,}", line):
            if columns:
                expect_data = not expect_data
            continue

        # Capture data rows
        if expect_data and columns and line.strip():
            parts = line.split()
            if len(parts) >= 16 and parts[0].isdigit():  # At least up to Aff(nM) and starts with Pos
                aff_nm = parts[15]
                bindlevel = ' '.join(parts[16:]) if len(parts) > 16 else None
                row = parts[:15] + [aff_nm, bindlevel]
                rows.append(row)

# Create DataFrame
df = pd.DataFrame(rows, columns=columns)

# Convert Pos to int for sorting
df['Pos'] = df['Pos'].astype(int)

# Convert numeric columns safely
for col in ["Aff(nM)", "%Rank_BA", "%Rank", "Score", "Score_BA"]:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

# Sheet 1: All peptides (sorted by Pos)
df_all = df.copy().sort_values(by='Pos')

# Sheet 2: Strong binders (sorted by Pos)
strong_binders = df[df["BindLevel"].str.contains("SB", na=False)].sort_values(by='Pos')

# Sheet 3: Weak binders (sorted by Pos)
weak_binders = df[df["BindLevel"].str.contains("WB", na=False)].sort_values(by='Pos')

# Sheet 4: Overlapping strong binders across alleles (sorted by Pos)
overlapping_strong = (
    strong_binders.groupby("Peptide")
    .filter(lambda x: x["MHC"].nunique() > 1)
    .sort_values(by='Pos')
)

# Sheet 5: Overlapping weak binders across alleles (sorted by Pos)
overlapping_weak = (
    weak_binders.groupby("Peptide")
    .filter(lambda x: x["MHC"].nunique() > 1)
    .sort_values(by='Pos')
)

# Write to Excel
with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    df_all.to_excel(writer, sheet_name="All_Peptides", index=False)
    strong_binders.to_excel(writer, sheet_name="Strong_Binders", index=False)
    weak_binders.to_excel(writer, sheet_name="Weak_Binders", index=False)
    overlapping_strong.to_excel(writer, sheet_name="Overlapping_Strong_Binders", index=False)
    overlapping_weak.to_excel(writer, sheet_name="Overlapping_Weak_Binders", index=False)

print(f"Total rows parsed: {len(rows)}")
print(f"Excel file saved successfully as: {output_file}")

# Create second Excel file with unique overlapping strong binders peptides, their identities, and MHC alleles
overlapping_strong_unique = (
    overlapping_strong.groupby('Peptide').agg({
        'Identity': 'first',
        'MHC': lambda x: ', '.join(sorted(x.unique()))
    }).reset_index().sort_values(by='Peptide')
)
second_output_file = "strong_binders_peptides.xlsx"

with pd.ExcelWriter(second_output_file, engine="openpyxl") as writer:
    overlapping_strong_unique.to_excel(writer, sheet_name="Strong_Binders_Peptides", index=False)

print(f"Second Excel file saved successfully as: {second_output_file}")

# Fetch FASTA for unique identities
def fetch_fasta(uniprot_id):
    # Parse ID: if starts with sp_, take the accession part
    if uniprot_id.startswith('sp_'):
        accession = uniprot_id.split('_')[1]
    else:
        accession = uniprot_id
    url = f'https://www.uniprot.org/uniprot/{accession}.fasta'
    response = requests.get(url)
    if response.status_code == 200:
        return response.text
    else:
        print(f"Failed to fetch {uniprot_id} (accession: {accession})")
        return None

unique_ids = overlapping_strong_unique['Identity'].drop_duplicates().tolist()
fasta_file = "proteins.fasta"
with open(fasta_file, 'w') as f:
    for uid in unique_ids:
        fasta = fetch_fasta(uid)
        if fasta:
            f.write(fasta)
        time.sleep(0.1)  # To be polite
print(f"FASTA file saved as: {fasta_file}")

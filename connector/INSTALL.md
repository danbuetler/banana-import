# Universal CSV Connector — Installation

## Install in Banana Accounting

1. Open Banana Accounting
2. Menu → **Tools** → **Import Extensions...**
3. Click the **+** button (or "Add from file")
4. Select `ch.banana.filter.import.universal.csv.js`
5. Done — it appears in the import list as **"Universal Bank Statement - Import any .csv / .txt"**

### Alternative (manual install)
Copy the `.js` file to:
- **macOS:** `~/Library/Application Support/Banana.ch/Banana9/Extensions/`
- **Windows:** `%APPDATA%\Banana.ch\Banana9\Extensions\`

Then restart Banana.

## Usage

1. In Banana: **File** → **Import into accounting...**
2. Select **Universal Bank Statement - Import any .csv / .txt**
3. Browse to your bank statement file (CSV or TXT from any bank)
4. Set the destination account and options in the dialog
5. Click OK

## Supported formats

Any CSV/TXT bank export with recognisable column headers in DE/EN/FR, including:
- PostFinance, UBS, Raiffeisen, ZKB, Migros Bank, Valiant
- Revolut, Wise, PayPal
- Any export with Date + Description + Amount columns

## Tip: Use with Banana Import Converter

For PDF or XLSX bank statements:
1. Convert them to CSV at **http://localhost:8500** (Banana Import Converter)
2. Import the downloaded CSV via this connector

// @id = ch.banana.filter.import.universal.csv
// @api = 1.0
// @pubdate = 2026-05-23-g
// @publisher = danbuetler
// @description = Universal Bank Statement - Import any .csv / .txt
// @description.de = Universal Kontoauszug - Beliebige .csv / .txt importieren
// @description.fr = Relevé bancaire universel - Importer n'importe quel .csv / .txt
// @doctype = *
// @docproperties =
// @task = import.transactions
// @outputformat = transactions.simple
// @inputdatasource = openfiledialog
// @inputfilefilter = Text files (*.txt *.csv);;All files (*.*)
// @inputfilefilter.de = Text (*.txt *.csv);;Alle Dateien (*.*)
// @inputfilefilter.fr = Texte (*.txt *.csv);;Tous (*.*)
// @inputfilefilter.it = Testo (*.txt *.csv);;Tutti i files (*.*)

/**
 * Universal Bank Statement Import Connector
 *
 * Works with any CSV/TXT bank export — no bank-specific code.
 * Auto-detects: separator, header row, date format, and column roles
 * by keyword matching against common DE/EN/FR bank column names.
 *
 * Output: Date | DateValue | Doc | Description | Income | Expenses
 */

function exec(inString, isTest) {

   // Banana probes exec() with empty inString — return nothing so Banana does not open
   // a second import dialog
   if (!inString || inString.trim().length === 0) {
      return '';
   }

   // Remove UTF-8 BOM if present
   if (inString.charCodeAt(0) === 0xFEFF)
      inString = inString.slice(1);

   // Normalise line endings (CRLF → LF) so csvToArray splits rows correctly
   inString = inString.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

   // Try all separators and use whichever produces the most rows
   var allRows = bestParse(inString);

   if (!allRows || allRows.length < 2)
      return "@Error: File appears empty or has no data rows. Supported formats: CSV, TXT with ; , or tab separators.";

   // Find the header row (first 20 rows scanned)
   var headerIdx = findHeaderRow(allRows);
   if (headerIdx < 0)
      return "@Error: Could not detect column headers.";

   var headers = allRows[headerIdx].map(function (h) { return h.trim(); });
   var colMap = mapColumns(headers);

   if (colMap.date < 0)
      return "@Error: No date column found. Ensure the file has a recognisable date column.";

   // Detect date format from first real data rows
   var dateFormat = detectDateFormat(allRows, headerIdx + 1, colMap.date);

   var transactionsToImport = [];

   for (var i = headerIdx + 1; i < allRows.length; i++) {
      var row = allRows[i];
      if (!row || row.length === 0 || (row.length === 1 && !row[0].trim())) continue;

      var dateRaw = colMap.date >= 0 && row[colMap.date] ? row[colMap.date].trim() : '';
      if (!dateRaw || !looksLikeDate(dateRaw)) continue;

      // Description — join multiple matched description columns if present
      var descParts = [];
      for (var j = 0; j < headers.length; j++) {
         if (getRole(headers[j]) === 'description' && row[j]) {
            var part = row[j].trim();
            if (part) descParts.push(part);
         }
      }
      var desc = descParts.join(' | ').replace(/ {2,}/g, ' ');

      var income = '';
      var expenses = '';

      if (colMap.income >= 0 && colMap.expenses >= 0) {
         income   = row[colMap.income]   ? cleanAmount(row[colMap.income].trim())   : '';
         expenses = row[colMap.expenses] ? cleanAmount(row[colMap.expenses].trim()) : '';
      } else if (colMap.amount >= 0 && row[colMap.amount]) {
         var rawAmt = cleanAmount(row[colMap.amount].trim());
         var amt = parseFloat(rawAmt);
         if (!isNaN(amt)) {
            if (amt >= 0) income   = String(Math.abs(amt));
            else          expenses = String(Math.abs(amt));
         }
      }

      transactionsToImport.push([
         toInternalDate(dateRaw, dateFormat),
         '',  // DateValue
         '',  // Doc
         desc,
         income   ? Banana.Converter.toInternalNumberFormat(income,   '.') : '',
         expenses ? Banana.Converter.toInternalNumberFormat(expenses, '.') : '',
      ]);
   }

   if (transactionsToImport.length === 0)
      return "@Error: No transactions could be parsed. Check that date and amount columns exist.";

   // Sort ascending by date (oldest first)
   transactionsToImport.sort(function (a, b) {
      return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0;
   });

   var header = [["Date", "DateValue", "Doc", "Description", "Income", "Expenses"]];
   return Banana.Converter.arrayToTsv(header.concat(transactionsToImport));
}


// ── Column detection ─────────────────────────────────────────────────────────

var ROLE_KEYWORDS = {
   date: [
      'datum', 'date', 'buchungsdatum', 'valutadatum', 'wertstellung', 'abschluss',
      'started date', 'completed date', 'booking date', 'value date',
      'date comptable', 'date de valeur', 'data contabile', 'data valuta'
   ],
   description: [
      'beschreibung', 'buchungstext', 'text', 'avisierungstext', 'mitteilung',
      'description', 'details', 'memo', 'narration', 'transaction details',
      'verwendungszweck', 'texte', 'libellé', 'causale', 'informationen', 'information'
   ],
   amount: [
      'betrag', 'amount', 'umsatz', 'netto', 'montant', 'importo',
      'transaction amount', 'total amount'
   ],
   income: [
      'gutschrift', 'einnahme', 'income', 'credit amount', 'credit amt',
      'credit', 'haben', 'eingang', 'crédit', 'accredito',
      'money in', 'paid in', 'deposits', 'payments in'
   ],
   expenses: [
      'lastschrift', 'belastung', 'ausgabe', 'expenses', 'debit amount', 'debit amt',
      'debit', 'soll', 'ausgang', 'débit', 'addebito',
      'money out', 'paid out', 'withdrawals', 'payments out'
   ],
};

function getRole(header) {
   var h = header.toLowerCase().trim();
   for (var role in ROLE_KEYWORDS) {
      var kws = ROLE_KEYWORDS[role];
      for (var k = 0; k < kws.length; k++) {
         if (h === kws[k] || h.indexOf(kws[k]) >= 0)
            return role;
      }
   }
   return null;
}

// Columns that look like account IDs or currency labels — never treat as amounts
var IDENTIFIER_RE = /account|konto|currency|iban|nummer|number|whg/;

function mapColumns(headers) {
   var result = { date: -1, description: -1, amount: -1, income: -1, expenses: -1 };
   var assigned = {};

   // Priority order: date first, then others
   var roleOrder = ['date', 'income', 'expenses', 'amount', 'description'];
   for (var ri = 0; ri < roleOrder.length; ri++) {
      var role = roleOrder[ri];
      var kws = ROLE_KEYWORDS[role];
      for (var j = 0; j < headers.length; j++) {
         if (assigned[j]) continue;
         var h = headers[j].toLowerCase().trim();
         // Never assign account/currency/identifier columns to financial amount roles
         if ((role === 'income' || role === 'expenses' || role === 'amount') && IDENTIFIER_RE.test(h)) continue;
         for (var k = 0; k < kws.length; k++) {
            if (h === kws[k] || h.indexOf(kws[k]) >= 0) {
               result[role] = j;
               assigned[j] = true;
               break;
            }
         }
         if (result[role] >= 0) break;
      }
   }

   return result;
}

function findHeaderRow(rows) {
   var allKws = [];
   for (var role in ROLE_KEYWORDS)
      allKws = allKws.concat(ROLE_KEYWORDS[role]);

   var bestIdx = 0;
   var bestScore = 0;

   for (var i = 0; i < Math.min(rows.length, 20); i++) {
      var score = 0;
      for (var j = 0; j < rows[i].length; j++) {
         var cell = rows[i][j].toLowerCase().trim();
         for (var k = 0; k < allKws.length; k++) {
            if (cell === allKws[k] || cell.indexOf(allKws[k]) >= 0) {
               score++;
               break;
            }
         }
      }
      if (score > bestScore) {
         bestScore = score;
         bestIdx = i;
      }
   }

   return bestScore > 0 ? bestIdx : -1;
}


// ── Date handling ─────────────────────────────────────────────────────────────

function detectDateFormat(rows, startIdx, col) {
   for (var i = startIdx; i < Math.min(rows.length, startIdx + 10); i++) {
      var v = rows[i][col] ? rows[i][col].trim().split(/\s+/)[0] : '';
      if (!v) continue;
      if (v.match(/^\d{2}\.\d{2}\.\d{4}$/)) return 'dd.mm.yyyy';
      if (v.match(/^\d{4}-\d{2}-\d{2}$/))   return 'yyyy-mm-dd';
      if (v.match(/^\d{2}\/\d{2}\/\d{4}$/)) return 'dd/mm/yyyy';
      if (v.match(/^\d{1,2}\/\d{1,2}\/\d{2,4}$/)) return 'mm/dd/yyyy';
      if (v.match(/^\d{2}-\d{2}-\d{4}$/))   return 'dd-mm-yyyy';
   }
   return 'dd.mm.yyyy';
}

function toInternalDate(raw, fmt) {
   var s = raw.trim().split(/\s+/)[0];
   if (fmt === 'yyyy-mm-dd') return s;
   var p, sep;
   if (fmt === 'dd.mm.yyyy') {
      p = s.split('.');
      if (p.length === 3) return p[2] + '-' + pad2(p[1]) + '-' + pad2(p[0]);
   }
   if (fmt === 'dd/mm/yyyy') {
      p = s.split('/');
      if (p.length === 3) return p[2] + '-' + pad2(p[1]) + '-' + pad2(p[0]);
   }
   if (fmt === 'dd-mm-yyyy') {
      p = s.split('-');
      if (p.length === 3) return p[2] + '-' + pad2(p[1]) + '-' + pad2(p[0]);
   }
   if (fmt === 'mm/dd/yyyy') {
      p = s.split('/');
      if (p.length === 3) return p[2] + '-' + pad2(p[0]) + '-' + pad2(p[1]);
   }
   return Banana.Converter.toInternalDateFormat(s, fmt);
}

function looksLikeDate(s) {
   return /\d{1,4}[.\-\/]\d{1,2}[.\-\/]\d{2,4}/.test(s);
}

function pad2(n) {
   return n.length === 1 ? '0' + n : n;
}


// ── Amount handling ───────────────────────────────────────────────────────────

function cleanAmount(s) {
   if (!s) return '';
   // Remove Swiss apostrophe and non-breaking space thousands separators
   s = s.replace(/['’ ]/g, '');
   // Determine decimal separator
   if (s.indexOf(',') >= 0 && s.indexOf('.') >= 0) {
      if (s.lastIndexOf('.') > s.lastIndexOf(','))
         s = s.replace(/,/g, '');            // comma = thousands
      else
         s = s.replace(/\./g, '').replace(',', '.');  // dot = thousands
   } else if (s.indexOf(',') >= 0) {
      s = s.replace(',', '.');               // European decimal comma
   }
   return s.replace(/[^0-9.\-]/g, '');
}


// ── Separator detection ───────────────────────────────────────────────────────

function bestParse(str) {
   // Try each separator; score = most-frequent column count × its frequency.
   // This rewards consistent structure and avoids picking a separator that
   // produces many columns in just one row (e.g. commas inside unquoted text).
   var separators = [';', ',', '\t', '|'];
   var best = null;
   var bestScore = 0;

   for (var si = 0; si < separators.length; si++) {
      var sep = separators[si];
      var rows = Banana.Converter.csvToArray(str, sep, '"');
      if (!rows || rows.length < 2) continue;

      var lenCount = {};
      for (var i = 0; i < Math.min(rows.length, 20); i++) {
         var l = rows[i].length;
         lenCount[l] = (lenCount[l] || 0) + 1;
      }

      // Find the column count that appears most often
      var topCols = 1;
      var topFreq = 0;
      for (var k in lenCount) {
         var cols = parseInt(k, 10);
         if (lenCount[k] > topFreq || (lenCount[k] === topFreq && cols > topCols)) {
            topFreq = lenCount[k];
            topCols = cols;
         }
      }
      var score = topCols * topFreq;  // e.g. 6 cols × 10 rows = 60

      if (score > bestScore) {
         bestScore = score;
         best = rows;
      }
   }

   return best;
}



# Example: writing a big list into one Google Sheet from a script

Found while building a list script for an Oppizi agent (lead-finder, 15–16 Oct 2026), each one
proven against real Sheets. Use it as a model for any destination: learn its rules, then make
your local fakes follow them.

Tools used (names from that agent; copy yours from its tool list):
`mcp_google-drive_import_to_google_sheets` (`file_name`, `content` as CSV, `source_format`)
creates a Sheet from CSV text. `mcp_google-sheets_modify_sheet_values` (`spreadsheet_id`,
`range_name`, `values`, `value_input_option`) writes cells into an existing Sheet.

## The traps

- **A new Sheet has about 1,000 rows of grid.** A write below it fails with "Range exceeds
  grid limits". If the list comes in parts, pad the first import with blank rows for the whole
  list (`","*(columns-1) + "\n"` per row), then write each later part into its exact range
  (`A<first>:<last col><last>`). The script works out every range itself; nothing is guessed.
- **The import turns `02134` into the number 2134.** That breaks zip codes in MA, CT, RI, NH,
  ME, VT, NJ and PR. Right after the import, rewrite the zip column of the first part as text
  with `value_input_option="RAW"`.
- **The write tool takes text cells only.** A number in `values` is refused as invalid
  arguments, which the script sees as "Tool arguments were rejected by effect policy". Send
  every cell as text with `USER_ENTERED`: a number's text comes back as a number (ratings, review
  counts), and every other cell gets a leading `'` so Google keeps it exactly (zips stay intact,
  and a cell starting with `=` is never a formula).
- **Get the Sheet id from the import's reply** with a regex on
  `https://docs.google.com/spreadsheets/d/<id>`. No link back means the Sheet wasn't made: return
  an error, and save the CSV only as the fallback.
- **Confirm each write from its reply.** Count a part as written only when the reply says how
  many rows (or cells) were updated and that matches what you sent. "ok", an error text, or no
  count is a failed write: return the error with the state from before that part.
- **One Sheet per list.** Count the Sheets your fake creates: a resumed run must write into the
  same Sheet (keep its id in `state`), never make a second one.

## Fakes that follow these rules

Your fake import parses the CSV, turns all-digit cells into numbers (as Google does), and
records a grid of `max(1000, rows)`. Your fake write refuses a range below the grid and any
non-text cell, and applies `USER_ENTERED` (strip a leading `'`, turn number text into numbers).
Then assert: one Sheet, no blank rows left, the row count matches the script's count, every
zip is 5 characters of text, and numbers came back as numbers.

## Testing for real

Test once in an area where zips start with 0 (a Texas or New York test never shows the zip
trap), with the smallest list that still takes two parts.

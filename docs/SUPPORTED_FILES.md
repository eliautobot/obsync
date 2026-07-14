# Supported files

| Type | Extensions | Extractor | Notes |
|---|---|---|---|
| Plain text and Markdown | `.txt`, `.md`, `.rst`, `.log` | Built in | Encoding detection includes UTF-8, UTF-16, Windows-1252, and Latin-1 |
| Structured text | `.json`, `.yaml`, `.toml`, `.xml`, `.ini` | Built in | JSON is formatted before analysis |
| Tables | `.csv`, `.tsv` | Built in | Delimiter is detected when possible |
| PDF | `.pdf` | pypdf | Text PDFs; scanned-only PDFs need a future PDF OCR pass |
| Microsoft Word | `.docx` | python-docx | Paragraphs and tables |
| Microsoft Excel | `.xlsx`, `.xlsm`, `.xltx` | openpyxl | Values only, read-only mode, bounded cells |
| Microsoft PowerPoint | `.pptx` | python-pptx | Text from slide shapes |
| Web pages | `.html`, `.htm` | Beautiful Soup | Scripts and styles removed |
| Email | `.eml` | Python email parser | Common headers and preferred body |
| Images | `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, `.webp` | Tesseract OCR | Included in the server Docker image |
| Source code | common code extensions | Built in | Stored as extracted plain text |
| Unknown/binary | any other extension | Metadata fallback | Filename, size, path, MIME type, and classification note are still synced |

Extraction is bounded by `OBSYNC_MAX_EXTRACT_CHARS` (default 200,000). Uploads are bounded by `OBSYNC_MAX_UPLOAD_MB` (default 100). Both limits are server-wide safety controls.

Legacy `.doc`, `.xls`, Outlook `.msg`, archives, audio transcription, and video analysis are roadmap items rather than silently partial parsers.


Checked before every push (the seller's list; Admin's list is appended when it lands):
- private/ (key maker), any real API token, voip id, extension, fax number, office name, logo, or config.json
- anything from the office that built it: names, city, colours, paths, screenshots with real faxes
- state folders (inbox/, outbox/, log.jsonl, audit.jsonl) or any fax PDF
- the licence signing secret is embedded in src/faxdesk/license.py by design (honour system); rotate the prefix if abused

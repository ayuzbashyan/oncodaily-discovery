import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from discovery import clinicaltrials, openalex, oncology_media_seeds, dedupe, score_record

FIELDS = ('name', 'category', 'relevance_score', 'kind', 'country', 'website',
          'description', 'source', 'source_url', 'evidence', 'rationale', 'evaluated_at',
          'competition_tier', 'parent_organization')


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def db_connect(path):
    conn = sqlite3.connect(path, factory=ClosingConnection)
    conn.execute('''CREATE TABLE IF NOT EXISTS organizations (
        identity TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT,
        relevance_score INTEGER, kind TEXT, country TEXT, website TEXT,
        description TEXT, source TEXT, source_url TEXT, evidence TEXT,
        rationale TEXT, evaluated_at TEXT, first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
        competition_tier TEXT DEFAULT '', parent_organization TEXT DEFAULT ''
    )''')
    # Existing user databases from prior ZIP versions are migrated in place.
    existing = {r[1] for r in conn.execute('PRAGMA table_info(organizations)')}
    for column in ('competition_tier', 'parent_organization'):
        if column not in existing:
            conn.execute(f'ALTER TABLE organizations ADD COLUMN {column} TEXT DEFAULT \'\'')
    conn.execute('CREATE INDEX IF NOT EXISTS organizations_rank ON organizations(relevance_score DESC)')
    conn.commit()
    return conn


def identity(row):
    website = (row.get('website') or '').strip()
    hostname = urlparse(website).netloc.lower().removeprefix('www.')
    # Public-web search snippets are unverified; use a title-specific key rather
    # than merging all candidates found on the same shared news domain.
    normalized = ''.join(ch for ch in row['name'].lower() if ch.isalnum())

    return 'name:' + normalized


def save(conn, results):
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for row in results:
        if row.get('country') != 'US':
            continue
        ident = identity(row)
        cols = [row.get(k, '') for k in FIELDS]
        conn.execute('''INSERT INTO organizations (
          identity, name, category, relevance_score, kind, country, website,
          description, source, source_url, evidence, rationale, evaluated_at,
          competition_tier, parent_organization,
          first_seen, last_seen, seen_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(identity) DO UPDATE SET
          name=excluded.name, category=excluded.category,
          relevance_score=excluded.relevance_score, kind=excluded.kind,
          country=excluded.country, website=excluded.website,
          description=excluded.description, source=excluded.source,
          source_url=excluded.source_url, evidence=excluded.evidence,
          rationale=excluded.rationale, evaluated_at=excluded.evaluated_at,
          competition_tier=excluded.competition_tier,
          parent_organization=excluded.parent_organization,
          last_seen=excluded.last_seen, seen_count=organizations.seen_count+1''',
          [ident, *cols, stamp, stamp])
    conn.commit()


def classify_record(record, specialty, preferred, model=None):
    base = score_record(record, specialty, 'Both')
    # Publisher-verified directory records remain competitor candidates even
    # when an optional early-stage ML model predicts another class.
    if model is not None and record.get('source') != 'US competitor directory':
        from ml.model import predict_record
        label, probability, distribution = predict_record(record, model)
        # Learned category; heuristic relevance score remains separate until a
        # separately labeled business-priority dataset exists.
        base['category'] = label
        base['rationale'] = (f'ML-predicted category (uncalibrated maximum class '
                             f'probability {probability:.1%}); requires human verification. '
                             f'Relevance score is still rule-based.')
        base['model_probabilities'] = distribution
    if preferred != 'Both' and base['category'] != preferred:
        return None
    return base


def collect(args):
    sources = []
    if getattr(args, 'include_trials', False):
        sources.append(('ClinicalTrials.gov', lambda: clinicaltrials(args.specialty, args.limit)))
    if not getattr(args, 'no_companies', False):
        sources.append(('OpenAlex US companies', lambda: openalex(args.specialty, args.limit, 'company')))
    if not getattr(args, 'no_media', False):
        sources.append(('US media directory', lambda: oncology_media_seeds(args.specialty, args.limit)))
    if not args.no_openalex:
        sources.append(('OpenAlex', lambda: openalex(args.specialty, args.limit)))
    if not sources:
        raise ValueError('Enable at least one data source')
    gathered = []
    for name, fetch in sources:
        try:
            records = fetch()
            gathered.extend(records)
            print(f'{name}: {len(records)} records', file=sys.stderr)
        except Exception as exc:
            print(f'{name} failed: {exc}', file=sys.stderr)
    if not gathered:
        return []
    model = None
    if getattr(args, 'model', None):
        from ml.model import load_model
        model = load_model(args.model)
    rows = [classify_record(record, args.specialty, args.category, model) for record in dedupe(keep_us_based(gathered))]
    rows = [x for x in rows if x is not None]
    if getattr(args,'direct_only',False):
        rows = [r for r in rows if r['category'] != 'Potential competitor' or r.get('competition_tier') == 'Direct']
    return sorted(rows, key=lambda r: r['relevance_score'], reverse=True)


def query(conn, category='Both', min_score=0, direct_only=False):
    sql = 'SELECT ' + ', '.join(FIELDS) + " FROM organizations WHERE relevance_score >= ? AND country = 'US'"
    params = [min_score]
    if category != 'Both':
        sql += ' AND category = ?'
        params.append(category)
    if direct_only:
        sql += " AND (category != 'Potential competitor' OR competition_tier = 'Direct')"
    sql += ' ORDER BY relevance_score DESC, name ASC'
    return [dict(zip(FIELDS, r)) for r in conn.execute(sql, params)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default='oncodaily.sqlite3', help='SQLite path')
    commands = parser.add_subparsers(dest='command', required=True)
    discover = commands.add_parser('discover', help='Fetch public datasets and save scored candidates')
    discover.add_argument('--specialty', default='oncology')
    discover.add_argument('--limit', type=int, default=25, help='Candidates fetched per source')
    discover.add_argument('--include-trials', action='store_true', help='Try trial sponsors; only records with US headquarters evidence are retained')
    discover.add_argument('--no-openalex', action='store_true', help='Exclude research institutions (default includes them)')
    discover.add_argument('--no-companies', action='store_true', help='Exclude OpenAlex company discovery')
    discover.add_argument('--no-media', action='store_true', help='Exclude the expanded US competitor directory')
    discover.add_argument('--model', default=None, help='Trusted trained .joblib classifier path')
    for command in (discover, commands.add_parser('list'), commands.add_parser('export')):
        command.add_argument('--direct-only', action='store_true', help='Omit adjacent competitor leads (keep potential customers if included)')
        command.add_argument('--category', choices=['Both', 'Potential customer', 'Potential competitor', 'Review required'], default='Both')
        command.add_argument('--min-score', type=int, default=0)
    export = commands.choices['export']
    export.add_argument('--output', default='oncodaily_organizations.csv')
    args = parser.parse_args(argv)
    if not 1 <= getattr(args, 'limit', 25) <= 100:
        parser.error('--limit must be between 1 and 100')
    if not 0 <= args.min_score <= 100:
        parser.error('--min-score must be between 0 and 100')
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_connect(str(db_path)) as conn:
        if args.command == 'discover':
            rows = collect(args)
            save(conn, rows)
            print(f'Saved/updated {len(rows)} candidates in {db_path}')
        elif args.command == 'list':
            for row in query(conn, args.category, args.min_score, args.direct_only):
                print(f"{row['relevance_score']:>3} | {row['category']:<20} | {row['name']} | {row['source_url']}")
        elif args.command == 'export':
            rows = query(conn, args.category, args.min_score, args.direct_only)
            with open(args.output, 'w', newline='', encoding='utf-8-sig') as out:
                output_fields = [field for field in FIELDS if field not in ("evidence", "location_evidence")]
                writer = csv.DictWriter(out, fieldnames=output_fields)
                writer.writeheader()
                writer.writerows({k: row.get(k, "") for k in output_fields} for row in rows)
            print(f'Exported {len(rows)} organizations to {args.output}')
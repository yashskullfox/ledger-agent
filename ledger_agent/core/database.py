"""
core/database.py  –  SQLite persistence layer (Repository pattern)
───────────────────────────────────────────────────────────────────
Uses plain sqlite3 (stdlib) so no ORM dependency is needed.
Schema is created / migrated on first connection via schema_version.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Generator, List, Optional

import os

from config import DB_PATH, DB_DIR
from ledger_agent.core.models import (
    Account, AccountSnapshot, AccountType, COAEntry, COAType,
    Entity, Position, PositionType, RealisedTrade, Transaction, TransactionType,
)

# Increment whenever the schema changes; auto-migration runs on connect.
# v4: positions.position_type (ARCH-25)
# v5: transactions.classifier_version, transactions.confidence (ARCH-27)
SCHEMA_VERSION = 6


def _decimal(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    """Resolve DB path, preferring FI_DB_PATH env var to support test isolation.

    Resolution order (highest priority first):
    1. Explicit ``db_path`` argument passed by the caller.
    2. ``FI_DB_PATH`` environment variable (set by test fixtures to a temp DB).
    3. ``DB_PATH`` captured from config at import time (production default).
    """
    if db_path is not None:
        return db_path
    env = os.environ.get("FI_DB_PATH", "").strip()
    if env:
        return Path(env)
    return DB_PATH


@contextmanager
def get_conn(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    db_path = _resolve_db_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_DDL = """
       CREATE TABLE IF NOT EXISTS schema_meta
       (
           key
           TEXT
           PRIMARY
           KEY,
           value
           TEXT
       );

       CREATE TABLE IF NOT EXISTS entities
       (
           id
           TEXT
           PRIMARY
           KEY,
           name
           TEXT
           NOT
           NULL,
           entity_type
           TEXT
           NOT
           NULL,
           state
           TEXT,
           ein_masked
           TEXT,
           notes
           TEXT
           DEFAULT
           '',
           created_at
           TEXT
           NOT
           NULL
       );

       CREATE TABLE IF NOT EXISTS accounts
       (
           id
           TEXT
           PRIMARY
           KEY,
           entity_id
           TEXT
           NOT
           NULL
           REFERENCES
           entities
       (
           id
       ),
           name TEXT NOT NULL,
           institution TEXT NOT NULL,
           account_type TEXT NOT NULL,
           account_number_masked TEXT NOT NULL,
           currency TEXT DEFAULT 'USD',
           is_active INTEGER DEFAULT 1,
           notes TEXT DEFAULT '',
           created_at TEXT NOT NULL
           );

       CREATE TABLE IF NOT EXISTS transactions
       (
           id
           TEXT
           PRIMARY
           KEY,
           account_id
           TEXT
           NOT
           NULL
           REFERENCES
           accounts
       (
           id
       ),
           date TEXT NOT NULL,
           description TEXT NOT NULL,
           raw_description TEXT DEFAULT '',
           amount TEXT NOT NULL,
           transaction_type TEXT NOT NULL,
           statement_period TEXT NOT NULL,
           coa_code TEXT DEFAULT '',
           coa_name TEXT DEFAULT '',
           tags TEXT DEFAULT '[]',
           notes TEXT DEFAULT '',
           is_reconciled INTEGER DEFAULT 0,
           is_transfer INTEGER DEFAULT 0,
           created_at TEXT NOT NULL
           );
       CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id);
       CREATE INDEX IF NOT EXISTS idx_tx_period ON transactions(statement_period);
       CREATE INDEX IF NOT EXISTS idx_tx_coa ON transactions(coa_code);

       CREATE TABLE IF NOT EXISTS positions
       (
           id
           TEXT
           PRIMARY
           KEY,
           account_id
           TEXT
           NOT
           NULL
           REFERENCES
           accounts
       (
           id
       ),
           symbol TEXT NOT NULL,
           name TEXT NOT NULL,
           quantity TEXT NOT NULL,
           price_per_unit TEXT NOT NULL,
           market_value TEXT NOT NULL,
           statement_period TEXT NOT NULL,
           cost_basis TEXT,
           unrealized_gain_loss TEXT,
           is_margin INTEGER DEFAULT 0,
           as_of_date TEXT,
           position_type TEXT DEFAULT 'equity',
           created_at TEXT NOT NULL
           );
       CREATE INDEX IF NOT EXISTS idx_pos_account ON positions(account_id);
       CREATE INDEX IF NOT EXISTS idx_pos_period ON positions(statement_period);

       CREATE TABLE IF NOT EXISTS account_snapshots
       (
           id
           TEXT
           PRIMARY
           KEY,
           account_id
           TEXT
           NOT
           NULL
           REFERENCES
           accounts
       (
           id
       ),
           statement_period TEXT NOT NULL,
           ending_balance TEXT NOT NULL,
           gross_asset_value TEXT,
           margin_balance TEXT,
           realised_gain_loss TEXT,
           beginning_balance TEXT,
           total_deposits TEXT,
           total_withdrawals TEXT,
           total_debits TEXT,
           total_credits TEXT,
           created_at TEXT NOT NULL,
           UNIQUE
       (
           account_id,
           statement_period
       )
           );

       CREATE TABLE IF NOT EXISTS realised_trades
       (
           id
           TEXT
           PRIMARY
           KEY,
           account_id
           TEXT
           NOT
           NULL
           REFERENCES
           accounts
       (
           id
       ),
           statement_period TEXT NOT NULL,
           symbol TEXT NOT NULL,
           description TEXT NOT NULL,
           gain_loss TEXT NOT NULL,
           term TEXT NOT NULL,
           settlement_date TEXT,
           created_at TEXT NOT NULL
           );
       CREATE INDEX IF NOT EXISTS idx_rt_period ON realised_trades(statement_period);

       CREATE TABLE IF NOT EXISTS coa
       (
           code
           TEXT
           PRIMARY
           KEY,
           name
           TEXT
           NOT
           NULL,
           coa_type
           TEXT
           NOT
           NULL,
           parent_code
           TEXT,
           description
           TEXT
           DEFAULT
           '',
           keywords
           TEXT
           DEFAULT
           '[]'
       );

       CREATE TABLE IF NOT EXISTS imported_statements
       (
           id
           TEXT
           PRIMARY
           KEY,
           source_file
           TEXT
           NOT
           NULL,
           parser_id
           TEXT
           NOT
           NULL,
           account_id
           TEXT
           NOT
           NULL
           REFERENCES
           accounts
       (
           id
       ),
           statement_period TEXT NOT NULL,
           imported_at TEXT NOT NULL,
           UNIQUE
       (
           account_id,
           statement_period
       )
           ); \
       """


def init_db(db_path: Optional[Path] = None) -> None:
    """Create tables and seed the COA if the DB is brand-new."""
    db_path = _resolve_db_path(db_path)
    with get_conn(db_path) as conn:
        conn.executescript(_DDL)
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()
        current = int(row["value"]) if row else 0
        if current < 4:
            # v4: add position_type column (ARCH-25); safe on new and existing DBs
            try:
                conn.execute(
                    "ALTER TABLE positions ADD COLUMN position_type TEXT DEFAULT 'equity'"
                )
            except Exception:
                pass  # column already exists on a freshly-created DB from the DDL above
        if current < 5:
            # v5: add classifier metadata columns (ARCH-27); nullable for legacy rows
            for col_def in (
                "ALTER TABLE transactions ADD COLUMN classifier_version TEXT",
                "ALTER TABLE transactions ADD COLUMN confidence TEXT",
            ):
                try:
                    conn.execute(col_def)
                except Exception:
                    pass  # column already exists on freshly-created DB
        if current < 6:
            try:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN classification_lock_at TEXT"
                )
            except Exception:
                pass  # column already exists on freshly-created DB
        if current < SCHEMA_VERSION:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('version',?)",
                (str(SCHEMA_VERSION),),
            )
    # Seed COA once
    _seed_coa(db_path)


_DEFAULT_COA: list[tuple] = [
    # (code, name, type, parent, description, keywords_json)
    # ── Assets ──────────────────────────────────────────────────────────────
    ("1000", "Cash & Cash Equivalents", "asset", None, "", '["cash","checking","deposit","balance"]'),
    ("1010", "Business Checking Account", "asset", "1000", "", '["bank_x","checking","intra_bank_xfer"]'),
    ("1100", "Investment & Brokerage Assets", "asset", None, "", '["broker_y","brokerage","investment"]'),
    ("1110", "Equity Securities (Long)", "asset", "1100", "", '["snap","cdna","caredx","bought","purchased"]'),  # redaction: allow
    ("1120", "Other Marketable Securities", "asset", "1100", "",
     '["kopin","ssr","kinross","solid power","bigbear","oscar","vale"]'),
    ("1200", "Accounts Receivable", "asset", None, "", '[]'),
    ("1300", "Prepaid Expenses", "asset", None, "", '[]'),
    # ── Liabilities ─────────────────────────────────────────────────────────
    ("2000", "Current Liabilities", "liability", None, "", '[]'),
    ("2010", "Margin Loan Payable", "liability", "2000", "", '["margin","debit balance"]'),
    ("2020", "Taxes Payable", "liability", "2000", "", '["irs","tax","usataxpymt"]'),
    ("2030", "Accounts Payable", "liability", "2000", "", '[]'),
    # ── Equity ──────────────────────────────────────────────────────────────
    ("3000", "Members Equity", "equity", None, "", '[]'),
    ("3010", "Members Capital Contributions", "equity", "3000", "", '["intra_bank_xfer","transfer","zelle","wire"]'),
    # V7 fix: ENTITY_A is a pass-through — federal/state estimated tax payments
    # made from the LLC account are partner draws, not entity-level tax expense.
    # Book them here (contra-equity) so they do NOT inflate Form 1065 deductions.
    ("3040", "Members Distributions / Owner Draws", "equity", "3000", "",
     '["irs","usataxpymt","estimated tax","owner draw","distribution","member draw"]'),
    ("3020", "Retained Earnings", "equity", "3000", "", '[]'),
    ("3030", "Current Period Net Income", "equity", "3000", "", '[]'),
    # ── Revenue ─────────────────────────────────────────────────────────────
    ("4000", "Revenue", "revenue", None, "", '[]'),
    ("4010", "Realised Trading Gains", "revenue", "4000", "", '["gain","sold","proceeds","realized gain"]'),
    ("4011", "Long-Term Capital Gain", "revenue", "4000", "", '["long-term gain","ltcg gain","ltcg"]'),
    ("4020", "Service Revenue", "revenue", "4000", "", '["intuit","deposit","invoice"]'),
    ("4021", "Dividend Income", "revenue", "4000", "", '["dividend","div reinv"]'),
    ("4030", "Other Income", "revenue", "4000", "", '[]'),
    ("4031", "Interest Income", "revenue", "4000", "", '["interest earned","interest credit"]'),
    # ── Expenses ────────────────────────────────────────────────────────────
    ("5000", "Operating Expenses", "expense", None, "", '[]'),
    ("5010", "Software & Subscriptions", "expense", "5000", "",
     '["quickbooks","google","subscription","recurring","saas","software","zoom","slack","github","adobe","dropbox","microsoft","azure","aws"]'),
    ("5020", "Bank & Transaction Fees", "expense", "5000", "",
     '["tran fee","service charge","fee","bank fee","nsf","wire fee","intuit tran"]'),
    ("5021", "Payroll & Wages", "expense", "5000", "", '["payroll","adp","gusto","wages","salary"]'),
    ("5030", "Margin Interest Expense", "expense", "5000", "", '["margin interest","interest paid"]'),
    ("5031", "Advertising & Marketing", "expense", "5000", "",
     '["meta ads","facebook ads","twitter ads","instagram ads","google ads"]'),
    ("5040", "Payroll Tax Expense", "expense", "5000", "", '["payroll tax","941","940"]'),
    ("5050", "Federal Income Tax Expense", "expense", "5000", "", '["irs","usataxpymt","federal tax"]'),
    ("5055", "State & Local Taxes", "expense", "5000", "", '["state tax","dept of rev","dept of revenue"]'),
    ("5060", "Investment Transaction Costs", "expense", "5000", "", '["transaction cost","commission"]'),
    ("5061", "Office & Shipping Supplies", "expense", "5000", "",
     '["office depot","staples","amazon","fedex","ups","usps","shipping"]'),
    ("5070", "Realised Trading Losses", "expense", "5000", "", '["loss","short-term loss","realized loss"]'),
    ("5071", "Legal & Professional Fees", "expense", "5000", "",
     '["incfile","registered agent","northwest registered","legalzoom","rocket lawyer","attorney","legal"]'),
    ("5075", "Long-Term Capital Loss", "expense", "5000", "", '["long-term loss","ltcg loss"]'),
    ("5080", "Other Operating Expenses", "expense", "5000", "", '[]'),
    ("5090", "Interest Expense", "expense", "5000", "", '["interest expense","loan interest"]'),
    ("5100", "Travel & Transportation", "expense", "5000", "",
     '["delta","united air","southwest","american air","uber","lyft","taxi","marriott","hilton","hyatt","airbnb"]'),
    ("5999", "Uncategorized Expense", "expense", "5000", "", '[]'),
    # ── Internal transfers (not P&L) ────────────────────────────────────────
    ("9000", "Inter-Account Transfer", "equity", None, "",
     '["intra_bank_xfer","transfer","zelle","wire transfer"]'),
]


def _seed_coa(db_path: Optional[Path] = None) -> None:
    """Insert canonical COA rows. INSERT OR IGNORE means safe to call any time —
    existing rows are preserved; new codes are added to live databases too."""
    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO coa(code,name,coa_type,parent_code,description,keywords)"
            " VALUES(?,?,?,?,?,?)",
            _DEFAULT_COA,
        )


class EntityRepo:
    @staticmethod
    def upsert(e: Entity, db_path: Optional[Path] = None) -> None:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entities(id,name,entity_type,state,ein_masked,notes,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (e.id, e.name, e.entity_type, e.state, e.ein_masked,
                 e.notes, e.created_at.isoformat()),
            )

    @staticmethod
    def get_by_name(name: str, db_path: Optional[Path] = None) -> Optional[Entity]:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM entities WHERE name=? LIMIT 1", (name,)
            ).fetchone()
        if row is None:
            return None
        return Entity(
            id=row["id"], name=row["name"], entity_type=row["entity_type"],
            state=row["state"] or "", ein_masked=row["ein_masked"],
            notes=row["notes"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def list_all(db_path: Optional[Path] = None) -> List[Entity]:
        with get_conn(db_path) as conn:
            rows = conn.execute("SELECT * FROM entities ORDER BY name").fetchall()
        return [Entity(id=r["id"], name=r["name"], entity_type=r["entity_type"],
                       state=r["state"] or "", ein_masked=r["ein_masked"],
                       notes=r["notes"] or "",
                       created_at=datetime.fromisoformat(r["created_at"])) for r in rows]


class AccountRepo:
    @staticmethod
    def upsert(a: Account, db_path: Optional[Path] = None) -> None:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO accounts"
                "(id,entity_id,name,institution,account_type,account_number_masked,"
                " currency,is_active,notes,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.entity_id, a.name, a.institution, a.account_type.value,
                 a.account_number_masked, a.currency, int(a.is_active),
                 a.notes, a.created_at.isoformat()),
            )

    @staticmethod
    def find(institution: str, account_number_masked: str,
             db_path: Optional[Path] = None) -> Optional[Account]:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE institution=? AND account_number_masked=? LIMIT 1",
                (institution, account_number_masked),
            ).fetchone()
        if row is None:
            return None
        return AccountRepo._row_to_model(row)

    @staticmethod
    def get_by_id(account_id: str, db_path: Optional[Path] = None) -> Optional[Account]:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id=? LIMIT 1", (account_id,)
            ).fetchone()
        return AccountRepo._row_to_model(row) if row else None

    @staticmethod
    def list_for_entity(entity_id: str, db_path: Optional[Path] = None) -> List[Account]:
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE entity_id=? ORDER BY institution,name",
                (entity_id,),
            ).fetchall()
        return [AccountRepo._row_to_model(r) for r in rows]

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"], entity_id=row["entity_id"],
            name=row["name"], institution=row["institution"],
            account_type=AccountType(row["account_type"]),
            account_number_masked=row["account_number_masked"],
            currency=row["currency"], is_active=bool(row["is_active"]),
            notes=row["notes"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class TransactionRepo:
    @staticmethod
    def bulk_insert(txns: List[Transaction], db_path: Optional[Path] = None) -> int:
        """
        Insert new transactions, skipping true duplicates.

        Dedup strategy: (account_id, date, description, amount, occurrence_index).
        `occurrence_index` counts how many times the same (account_id, date,
        description, amount) tuple already exists in the DB before this call,
        so a second *identical* line in a bank statement (e.g. two payroll ACH
        debits for exactly the same amount on the same day) can still be inserted
        on first import, while a re-import of the same PDF will correctly skip it.

        Within a single call the seen-counter tracks same-tuple repetitions in
        the incoming list, ensuring they are matched to the correct occurrence.
        """
        inserted = 0
        # Track how many times each (acct,date,desc,amt) tuple appears in this batch
        seen_in_batch: dict = {}
        with get_conn(db_path) as conn:
            for t in txns:
                key = (t.account_id, t.date.isoformat(), t.description, str(t.amount))
                batch_idx = seen_in_batch.get(key, 0)
                seen_in_batch[key] = batch_idx + 1

                # Count how many of this exact tuple already exist in the DB
                existing_count = conn.execute(
                    "SELECT COUNT(*) FROM transactions WHERE account_id=? AND date=?"
                    " AND description=? AND amount=?",
                    key,
                ).fetchone()[0]

                # Skip only if the DB already has at least (batch_idx + 1) copies
                if existing_count > batch_idx:
                    continue
                conn.execute(
                    "INSERT INTO transactions(id,account_id,date,description,raw_description,"
                    "amount,transaction_type,statement_period,coa_code,coa_name,tags,notes,"
                    "is_reconciled,is_transfer,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t.id, t.account_id, t.date.isoformat(), t.description,
                     t.raw_description, str(t.amount), t.transaction_type.value,
                     t.statement_period, t.coa_code, t.coa_name,
                     json.dumps(t.tags), t.notes,
                     int(t.is_reconciled), int(t.is_transfer),
                     t.created_at.isoformat()),
                )
                inserted += 1
        return inserted

    @staticmethod
    def update_coa(tx_id: str, coa_code: str, coa_name: str,
                   db_path: Optional[Path] = None) -> None:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE transactions SET coa_code=?, coa_name=? WHERE id=?",
                (coa_code, coa_name, tx_id),
            )

    @staticmethod
    def update_coa_with_meta(
        tx_id: str,
        coa_code: str,
        coa_name: str,
        classifier_version: str,
        confidence: float,
        db_path: Optional[Path] = None,
    ) -> None:
        """R-65/R-66 / ARCH-27: persist classification with audit metadata."""
        from datetime import datetime, timezone
        lock_ts = datetime.now(timezone.utc).isoformat()
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE transactions"
                " SET coa_code=?, coa_name=?, classifier_version=?, confidence=?,"
                " classification_lock_at=?"
                " WHERE id=?",
                (coa_code, coa_name, classifier_version, str(confidence), lock_ts, tx_id),
            )

    @staticmethod
    def get_by_id(tx_id: str, db_path: Optional[Path] = None) -> Optional["Transaction"]:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE id=? LIMIT 1", (tx_id,)
            ).fetchone()
        return TransactionRepo._row_to_model(row) if row else None

    @staticmethod
    def list_for_period(statement_period: str,
                        account_id: Optional[str] = None,
                        db_path: Optional[Path] = None) -> List[Transaction]:
        with get_conn(db_path) as conn:
            if account_id:
                rows = conn.execute(
                    "SELECT * FROM transactions WHERE statement_period=? AND account_id=?"
                    " ORDER BY date",
                    (statement_period, account_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM transactions WHERE statement_period=? ORDER BY date",
                    (statement_period,),
                ).fetchall()
        return [TransactionRepo._row_to_model(r) for r in rows]

    @staticmethod
    def list_unclassified(db_path: Optional[Path] = None) -> List[Transaction]:
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE (coa_code='' OR coa_code IS NULL)"
                " ORDER BY date"
            ).fetchall()
        return [TransactionRepo._row_to_model(r) for r in rows]

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> Transaction:
        return Transaction(
            id=row["id"], account_id=row["account_id"],
            date=date.fromisoformat(row["date"]),
            description=row["description"],
            raw_description=row["raw_description"] or "",
            amount=_decimal(row["amount"]),
            transaction_type=TransactionType(row["transaction_type"]),
            statement_period=row["statement_period"],
            coa_code=row["coa_code"] or "",
            coa_name=row["coa_name"] or "",
            tags=json.loads(row["tags"] or "[]"),
            notes=row["notes"] or "",
            is_reconciled=bool(row["is_reconciled"]),
            is_transfer=bool(row["is_transfer"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class PositionRepo:
    @staticmethod
    def upsert_period(positions: List[Position], db_path: Optional[Path] = None) -> None:
        if not positions:
            return
        period = positions[0].statement_period
        acct_id = positions[0].account_id
        with get_conn(db_path) as conn:
            conn.execute(
                "DELETE FROM positions WHERE account_id=? AND statement_period=?",
                (acct_id, period),
            )
            for p in positions:
                conn.execute(
                    "INSERT INTO positions(id,account_id,symbol,name,quantity,price_per_unit,"
                    "market_value,statement_period,cost_basis,unrealized_gain_loss,"
                    "is_margin,as_of_date,position_type,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p.id, p.account_id, p.symbol, p.name, str(p.quantity),
                     str(p.price_per_unit), str(p.market_value), p.statement_period,
                     str(p.cost_basis) if p.cost_basis is not None else None,
                     str(p.unrealized_gain_loss) if p.unrealized_gain_loss is not None else None,
                     int(p.is_margin),
                     p.as_of_date.isoformat() if p.as_of_date else None,
                     p.position_type.value,
                     p.created_at.isoformat()),
                )

    @staticmethod
    def list_for_period(account_id: str, statement_period: str,
                        db_path: Optional[Path] = None) -> List[Position]:
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE account_id=? AND statement_period=?"
                " ORDER BY symbol",
                (account_id, statement_period),
            ).fetchall()
        return [PositionRepo._row_to_model(r) for r in rows]

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> Position:
        return Position(
            id=row["id"], account_id=row["account_id"],
            symbol=row["symbol"], name=row["name"],
            quantity=_decimal(row["quantity"]),
            price_per_unit=_decimal(row["price_per_unit"]),
            market_value=_decimal(row["market_value"]),
            statement_period=row["statement_period"],
            cost_basis=_decimal(row["cost_basis"]) if row["cost_basis"] else None,
            unrealized_gain_loss=_decimal(row["unrealized_gain_loss"])
            if row["unrealized_gain_loss"] else None,
            is_margin=bool(row["is_margin"]),
            as_of_date=_date(row["as_of_date"]),
            position_type=PositionType(row["position_type"] or "equity"),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class SnapshotRepo:
    @staticmethod
    def upsert(s: AccountSnapshot, db_path: Optional[Path] = None) -> None:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO account_snapshots"
                "(id,account_id,statement_period,ending_balance,gross_asset_value,"
                "margin_balance,realised_gain_loss,beginning_balance,total_deposits,"
                "total_withdrawals,total_debits,total_credits,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.id, s.account_id, s.statement_period, str(s.ending_balance),
                 str(s.gross_asset_value) if s.gross_asset_value is not None else None,
                 str(s.margin_balance) if s.margin_balance is not None else None,
                 str(s.realised_gain_loss) if s.realised_gain_loss is not None else None,
                 str(s.beginning_balance) if s.beginning_balance is not None else None,
                 str(s.total_deposits) if s.total_deposits is not None else None,
                 str(s.total_withdrawals) if s.total_withdrawals is not None else None,
                 str(s.total_debits) if s.total_debits is not None else None,
                 str(s.total_credits) if s.total_credits is not None else None,
                 s.created_at.isoformat()),
            )

    @staticmethod
    def get(account_id: str, period: str,
            db_path: Optional[Path] = None) -> Optional[AccountSnapshot]:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM account_snapshots WHERE account_id=? AND statement_period=?",
                (account_id, period),
            ).fetchone()
        if row is None:
            return None
        return SnapshotRepo._row_to_model(row)

    @staticmethod
    def list_for_entity(entity_id: str, db_path: Optional[Path] = None) -> List[AccountSnapshot]:
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT s.* FROM account_snapshots s"
                " JOIN accounts a ON a.id=s.account_id"
                " WHERE a.entity_id=?"
                " ORDER BY s.statement_period, a.institution",
                (entity_id,),
            ).fetchall()
        return [SnapshotRepo._row_to_model(r) for r in rows]

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> AccountSnapshot:
        def _d(k):
            return _decimal(row[k]) if row[k] is not None else None

        return AccountSnapshot(
            id=row["id"], account_id=row["account_id"],
            statement_period=row["statement_period"],
            ending_balance=_decimal(row["ending_balance"]),
            gross_asset_value=_d("gross_asset_value"),
            margin_balance=_d("margin_balance"),
            realised_gain_loss=_d("realised_gain_loss"),
            beginning_balance=_d("beginning_balance"),
            total_deposits=_d("total_deposits"),
            total_withdrawals=_d("total_withdrawals"),
            total_debits=_d("total_debits"),
            total_credits=_d("total_credits"),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class RealisedTradeRepo:
    @staticmethod
    def upsert_period(trades: List[RealisedTrade], db_path: Optional[Path] = None) -> None:
        if not trades:
            return
        period = trades[0].statement_period
        acct_id = trades[0].account_id
        with get_conn(db_path) as conn:
            conn.execute(
                "DELETE FROM realised_trades WHERE account_id=? AND statement_period=?",
                (acct_id, period),
            )
            for t in trades:
                conn.execute(
                    "INSERT INTO realised_trades(id,account_id,statement_period,symbol,"
                    "description,gain_loss,term,settlement_date,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (t.id, t.account_id, t.statement_period, t.symbol,
                     t.description, str(t.gain_loss), t.term,
                     t.settlement_date.isoformat() if t.settlement_date else None,
                     t.created_at.isoformat()),
                )

    @staticmethod
    def list_for_period(statement_period: str,
                        db_path: Optional[Path] = None) -> List[RealisedTrade]:
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM realised_trades WHERE statement_period=? ORDER BY settlement_date",
                (statement_period,),
            ).fetchall()
        return [RealisedTrade(
            id=r["id"], account_id=r["account_id"],
            statement_period=r["statement_period"], symbol=r["symbol"],
            description=r["description"], gain_loss=_decimal(r["gain_loss"]),
            term=r["term"],
            settlement_date=_date(r["settlement_date"]),
            created_at=datetime.fromisoformat(r["created_at"]),
        ) for r in rows]


class COARepo:
    @staticmethod
    def list_all(db_path: Optional[Path] = None) -> List[COAEntry]:
        with get_conn(db_path) as conn:
            rows = conn.execute("SELECT * FROM coa ORDER BY code").fetchall()
        return [COARepo._row_to_model(r) for r in rows]

    @staticmethod
    def get(code: str, db_path: Optional[Path] = None) -> Optional[COAEntry]:
        with get_conn(db_path) as conn:
            row = conn.execute("SELECT * FROM coa WHERE code=?", (code,)).fetchone()
        return COARepo._row_to_model(row) if row else None

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> COAEntry:
        return COAEntry(
            code=row["code"], name=row["name"],
            coa_type=COAType(row["coa_type"]),
            parent_code=row["parent_code"],
            description=row["description"] or "",
            keywords=json.loads(row["keywords"] or "[]"),
        )


class ImportRegistry:
    @staticmethod
    def already_imported(account_id: str, period: str,
                         db_path: Optional[Path] = None) -> bool:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT id FROM imported_statements WHERE account_id=? AND statement_period=?",
                (account_id, period),
            ).fetchone()
        return row is not None

    @staticmethod
    def record(source_file: str, parser_id: str, account_id: str, period: str,
               db_path: Optional[Path] = None) -> None:
        import uuid as _uuid
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO imported_statements"
                "(id,source_file,parser_id,account_id,statement_period,imported_at)"
                " VALUES(?,?,?,?,?,?)",
                (str(_uuid.uuid4()), source_file, parser_id, account_id,
                 period, datetime.now(timezone.utc).isoformat()),
            )

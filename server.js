require('dotenv').config();

const express  = require('express');
const session  = require('express-session');
const path     = require('path');
const { DatabaseSync } = require('node:sqlite');
const history  = require('./seed-history');

const app            = express();
const PORT           = process.env.PORT || 3000;
const PASSWORD       = process.env.APP_PASSWORD || 'DimmanComp8';
const SESSION_SECRET = process.env.SESSION_SECRET || 'fallback-secret-' + Date.now();

// ── Database ──────────────────────────────────────────────────────────────────
const dbPath = process.env.DB_PATH
  ? path.join(process.env.DB_PATH, 'comp.db')
  : path.join(__dirname, 'comp.db');

const db = new DatabaseSync(dbPath);
db.exec(`PRAGMA journal_mode = WAL`);

db.exec(`
  CREATE TABLE IF NOT EXISTS members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    UNIQUE NOT NULL,
    is_archived INTEGER DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  INTEGER NOT NULL,
    date       TEXT    NOT NULL,
    minutes    INTEGER NOT NULL,
    comment    TEXT,
    created_at TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (member_id) REFERENCES members(id)
  );
  CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
  );
`);

// Migrations for existing DBs
try { db.exec(`ALTER TABLE members ADD COLUMN is_archived INTEGER DEFAULT 0`); } catch {}
try { db.exec(`ALTER TABLE members ADD COLUMN nickname TEXT`); } catch {}

// Data fixes
try { db.exec(`UPDATE members SET name = 'Jennifer' WHERE name = 'Jen'`); } catch {}

// Set nicknames for existing members (only if not already set)
const nicknameMap = {
  'Henrik':   'G-Lover',
  'Chumu':    'Churro',
  'Joel':     'Jo-El',
  'Dmitry':   'Dimman',
  'Jennifer': 'Jenny from the Block',
};
const updateNick = db.prepare(`UPDATE members SET nickname = ? WHERE name = ? AND nickname IS NULL`);
for (const [name, nickname] of Object.entries(nicknameMap)) {
  updateNick.run(nickname, name);
}

// ── Full history seed (runs once) ─────────────────────────────────────────────
const seeded = db.prepare(`SELECT value FROM db_meta WHERE key = 'history_seeded'`).get();

if (!seeded) {
  const insertMember = db.prepare(`INSERT OR IGNORE INTO members (name, is_archived) VALUES (?, ?)`);
  const getMember    = db.prepare(`SELECT id FROM members WHERE name = ?`);
  const insertEntry  = db.prepare(
    `INSERT INTO entries (member_id, date, minutes, comment) VALUES (?, ?, ?, ?)`
  );
  const deleteStarter = db.prepare(
    `DELETE FROM entries WHERE comment = 'Startsaldo överfört från Excel'`
  );

  db.exec('BEGIN');
  try {
    deleteStarter.run();

    for (const m of history.active) {
      insertMember.run(m.name, 0);
      const { id } = getMember.get(m.name);
      for (const [date, mins, comment] of m.entries) {
        insertEntry.run(id, date, mins, comment || null);
      }
    }

    for (const m of history.archived) {
      insertMember.run(m.name, 1);
      const { id } = getMember.get(m.name);
      for (const [date, mins, comment] of m.entries) {
        insertEntry.run(id, date, mins, comment || null);
      }
    }

    db.prepare(`INSERT INTO db_meta (key, value) VALUES ('history_seeded', '1')`).run();
    db.exec('COMMIT');
  } catch (e) {
    db.exec('ROLLBACK');
    throw e;
  }
  console.log('History seeded.');
}

// ── Middleware ────────────────────────────────────────────────────────────────
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));
app.use(session({
  secret:            SESSION_SECRET,
  resave:            false,
  saveUninitialized: false,
  cookie:            { maxAge: 7 * 24 * 60 * 60 * 1000 },
}));

const requireAuth = (req, res, next) => {
  if (req.session.authenticated) return next();
  res.status(401).json({ error: 'Not authenticated' });
};

// ── Auth ──────────────────────────────────────────────────────────────────────
app.get('/api/auth/status', (req, res) => res.json({ authenticated: !!req.session.authenticated }));

app.post('/api/login', (req, res) => {
  if (req.body.password === PASSWORD) {
    req.session.authenticated = true;
    res.json({ ok: true });
  } else {
    res.status(401).json({ error: 'Fel lösenord' });
  }
});

app.post('/api/logout', (req, res) => { req.session.destroy(); res.json({ ok: true }); });

// ── Members ───────────────────────────────────────────────────────────────────
const memberBalanceSQL = `
  SELECT m.id, m.name, m.nickname, m.is_archived,
         COALESCE(SUM(e.minutes), 0) AS balance_minutes
  FROM   members m
  LEFT JOIN entries e ON m.id = e.member_id
  WHERE  m.is_archived = ?
  GROUP  BY m.id, m.name
  ORDER  BY balance_minutes DESC
`;

app.get('/api/members', requireAuth, (req, res) => {
  res.json(db.prepare(memberBalanceSQL).all(0));
});

app.get('/api/members/archived', requireAuth, (req, res) => {
  res.json(db.prepare(memberBalanceSQL).all(1));
});

app.post('/api/members', requireAuth, (req, res) => {
  const { name, nickname } = req.body;
  if (!name || !name.trim()) return res.status(400).json({ error: 'Namn saknas' });
  try {
    const { lastInsertRowid } = db.prepare(
      `INSERT INTO members (name, nickname, is_archived) VALUES (?, ?, 0)`
    ).run(name.trim(), nickname?.trim() || null);
    res.json({ id: lastInsertRowid });
  } catch {
    res.status(409).json({ error: 'En person med det namnet finns redan' });
  }
});

app.patch('/api/members/:id', requireAuth, (req, res) => {
  const { is_archived, nickname } = req.body;
  const id = Number(req.params.id);
  if (is_archived !== undefined) {
    db.prepare(`UPDATE members SET is_archived = ? WHERE id = ?`).run(Number(is_archived), id);
  }
  if (nickname !== undefined) {
    db.prepare(`UPDATE members SET nickname = ? WHERE id = ?`).run(nickname || null, id);
  }
  res.json({ ok: true });
});

// ── Entries ───────────────────────────────────────────────────────────────────
app.get('/api/entries', requireAuth, (req, res) => {
  const { member_id } = req.query;
  const base = `
    SELECT e.id, e.member_id, m.name AS member_name,
           e.date, e.minutes, e.comment, e.created_at
    FROM   entries e JOIN members m ON e.member_id = m.id
  `;
  if (member_id) {
    res.json(db.prepare(base + ' WHERE e.member_id = ? ORDER BY e.date DESC, e.created_at DESC').all(Number(member_id)));
  } else {
    res.json(db.prepare(base + ' ORDER BY e.date DESC, e.created_at DESC').all());
  }
});

app.post('/api/entries', requireAuth, (req, res) => {
  const { member_id, date, minutes, comment } = req.body;
  if (!member_id || !date || minutes === undefined || minutes === null)
    return res.status(400).json({ error: 'Saknade fält' });
  const { lastInsertRowid } = db.prepare(
    `INSERT INTO entries (member_id, date, minutes, comment) VALUES (?, ?, ?, ?)`
  ).run(Number(member_id), String(date), Number(minutes), comment || null);
  res.json({ id: lastInsertRowid });
});

app.delete('/api/entries/:id', requireAuth, (req, res) => {
  db.prepare(`DELETE FROM entries WHERE id = ?`).run(Number(req.params.id));
  res.json({ ok: true });
});

// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, () => console.log(`Komp Tracker → http://localhost:${PORT}`));

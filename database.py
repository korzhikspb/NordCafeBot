import aiosqlite

DB_PATH = 'registrations.db'

async def init_db():
    """Инициализация БД и мягкие миграции (seats, open_at)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        # События
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                date_time TEXT NOT NULL,
                place TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Регистрации
        await db.execute("""
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT,
                phone TEXT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
        """)

        # --- миграции ---
        # seats в registrations
        seats_exists = False
        async with db.execute("PRAGMA table_info(registrations)") as cur:
            cols = await cur.fetchall()
            for c in cols:
                if c[1] == "seats":
                    seats_exists = True
                    break
        if not seats_exists:
            await db.execute("ALTER TABLE registrations ADD COLUMN seats INTEGER DEFAULT 1")

        # open_at в events
        open_exists = False
        async with db.execute("PRAGMA table_info(events)") as cur:
            cols = await cur.fetchall()
            for c in cols:
                if c[1] == "open_at":
                    open_exists = True
                    break
        if not open_exists:
            await db.execute("ALTER TABLE events ADD COLUMN open_at TEXT")
            # старым событиям открываем сразу (open_at = date_time)
            await db.execute("UPDATE events SET open_at = date_time WHERE open_at IS NULL")

        await db.commit()

async def add_registration(event_id: int, user_id: int, name: str, phone: str, seats: int = 1):
    """Добавить новую запись на мероприятие."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO registrations (event_id, user_id, name, phone, seats) VALUES (?, ?, ?, ?, ?)",
            (event_id, user_id, name, phone, seats)
        )
        await db.commit()

async def create_event(name: str, description: str, date_time: str, place: str, open_at: str | None = None):
    """Добавить новое мероприятие. open_at — время открытия регистрации (если None, открыто сразу)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO events (name, description, date_time, place, open_at) VALUES (?, ?, ?, ?, ?)",
            (name, description, date_time, place, open_at or date_time)
        )
        await db.commit()

async def get_all_events():
    """Список всех мероприятий (для админов)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name, description, date_time, place FROM events "
            "ORDER BY date(date_time), time(date_time)"
        )
        return await cursor.fetchall()

async def get_visible_events(now_iso: str):
    """
    Список событий, видимых пользователю:
      - событие не в прошлом (date_time >= now_iso)
      - open_at наступил (open_at <= now_iso) или NULL
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name, description, date_time, place FROM events "
            "WHERE date_time >= ? AND (open_at IS NULL OR open_at <= ?) "
            "ORDER BY date(date_time), time(date_time)",
            (now_iso, now_iso)
        )
        return await cursor.fetchall()

async def get_event_by_id(event_id: int):
    """Мероприятие по ID (возвращает и open_at шестой колонкой)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name, description, date_time, place, open_at FROM events WHERE id = ?",
            (event_id,)
        )
        return await cursor.fetchone()

async def delete_event(event_id: int):
    """Удалить мероприятие по ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await db.commit()

async def delete_registrations_for_event(event_id: int):
    """Удалить все регистрации на мероприятие."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM registrations WHERE event_id = ?", (event_id,))
        await db.commit()

async def get_registrations_by_event(event_id: int):
    """Список регистраций для события (user_id, name, phone, seats)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, name, phone, COALESCE(seats, 1) as seats "
            "FROM registrations WHERE event_id = ?",
            (event_id,)
        )
        return await cursor.fetchall()

async def delete_registration(event_id: int, user_id: int):
    """Удалить одну регистрацию пользователя на мероприятие."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM registrations WHERE event_id = ? AND user_id = ?",
            (event_id, user_id)
        )
        await db.commit()
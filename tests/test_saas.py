import pytest
from datetime import datetime, timezone, timedelta
from cliper import db, auth


@pytest.fixture(autouse=True)
def setup_tmp_db(tmp_path, monkeypatch):
    test_db_path = tmp_path / "cliper_test.db"
    monkeypatch.setattr(db, "DB_PATH", test_db_path)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    db.init_db()


def test_user_creation_and_trial():
    pwd_hash, salt = auth.hash_password("secret123")
    user = db.create_user("test@example.com", pwd_hash, salt, trial_days=7)
    
    assert user["email"] == "test@example.com"
    assert user["is_trial_active"] is True
    assert user["is_pro"] is True
    assert "Pro Trial" in user["status_label"]


def test_password_verification():
    pwd_hash, salt = auth.hash_password("mySecurePassword")
    assert auth.verify_password("mySecurePassword", pwd_hash, salt) is True
    assert auth.verify_password("wrongPassword", pwd_hash, salt) is False


def test_quota_limits():
    pwd_hash, salt = auth.hash_password("secret123")
    user = db.create_user("freeuser@example.com", pwd_hash, salt, trial_days=0)
    
    # Manually expire trial for testing free tier limits
    expired_trial = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db.get_connection() as conn:
        conn.execute("UPDATE users SET trial_ends_at = ? WHERE id = ?", (expired_trial, user["id"]))
        conn.commit()
    
    # 1st analyze allowed
    quota1 = db.check_daily_quota(user["id"], "analyze")
    assert quota1["allowed"] is True
    db.record_usage(user["id"], "analyze")
    
    # 2nd analyze blocked on Free tier
    quota2 = db.check_daily_quota(user["id"], "analyze")
    assert quota2["allowed"] is False
    assert "Free tier limit reached" in quota2["reason"]


def test_job_isolation():
    pwd_hash, salt = auth.hash_password("secret123")
    u1 = db.create_user("user1@example.com", pwd_hash, salt)
    u2 = db.create_user("user2@example.com", pwd_hash, salt)
    
    db.link_job_to_user("job123", u1["id"], "https://youtube.com/watch?v=123", "User 1 Video")
    
    assert db.is_job_owned_by("job123", u1["id"]) is True
    assert db.is_job_owned_by("job123", u2["id"]) is False
    assert "job123" in db.get_user_job_ids(u1["id"])
    assert "job123" not in db.get_user_job_ids(u2["id"])

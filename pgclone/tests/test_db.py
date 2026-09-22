import pytest

from pgclone import db, exceptions


@pytest.mark.parametrize(
    "lock_timeout, statement_timeout, sql, expected_sql",
    [
        (None, None, "COMMAND", "COMMAND;"),
        (1, None, " COMMAND; ", "SET lock_timeout = 1;\nCOMMAND;"),
        (1, 2, " COMMAND; ", "SET lock_timeout = 1;\nSET statement_timeout = 2;\nCOMMAND;"),
        (None, 2, " COMMAND; ", "SET statement_timeout = 2;\nCOMMAND;"),
        (
            None,
            None,
            "    COMMAND\n      INDENTED\n      INDENTED; ",
            "COMMAND\n  INDENTED\n  INDENTED;",
        ),
    ],
)
def test_fmt_psql_sql(settings, lock_timeout, statement_timeout, sql, expected_sql):
    """Tests variations of the db._fmt_psql_sql command"""
    settings.PGCLONE_STATEMENT_TIMEOUT = statement_timeout
    settings.PGCLONE_LOCK_TIMEOUT = lock_timeout
    assert db._fmt_psql_sql(sql) == expected_sql


def test_kill_connections_sql_only_targets_current_user():
    sql = db._kill_connections_sql("postgres__temp")
    assert "pg_terminate_backend(pid)" in sql
    assert "datname = 'postgres__temp'" in sql
    assert "usename = CURRENT_USER" in sql


def test_kill_connections_ignores_errors(mocker):
    psql = mocker.patch("pgclone.db.psql", autospec=True)
    db._kill_connections({"NAME": "postgres__temp"}, using="default")
    psql.assert_called_once()
    assert psql.call_args.kwargs["ignore_errors"] is True
    assert "CURRENT_USER" in psql.call_args.args[0]


def test_psql_retries_exclusive_commands(mocker):
    mocker.patch("pgclone.db._kill_connections", autospec=True)
    mocker.patch("pgclone.db._EXCLUSIVE_ACCESS_DELAY_SECONDS", 0)
    exec_psql = mocker.patch(
        "pgclone.db._exec_psql",
        side_effect=[exceptions.RuntimeError("Error running command."), mocker.Mock()],
    )

    db.psql("ALTER DATABASE x RENAME TO y", using="default", kill_connections={"NAME": "x"})

    assert exec_psql.call_count == 2


def test_psql_retries_exhausted(mocker):
    mocker.patch("pgclone.db._kill_connections", autospec=True)
    mocker.patch("pgclone.db._EXCLUSIVE_ACCESS_DELAY_SECONDS", 0)
    mocker.patch("pgclone.db._EXCLUSIVE_ACCESS_RETRIES", 2)
    mocker.patch(
        "pgclone.db._exec_psql",
        side_effect=exceptions.RuntimeError("Error running command."),
    )

    with pytest.raises(exceptions.RuntimeError, match="Error running command"):
        db.psql("DROP DATABASE x", using="default", kill_connections={"NAME": "x"})


def test_disable_autovacuum(mocker):
    psql = mocker.patch("pgclone.db.psql", autospec=True)
    db.disable_autovacuum({"NAME": "postgres__temp"}, using="default")
    psql.assert_called_once_with(
        'ALTER DATABASE "postgres__temp" SET autovacuum = off',
        using="default",
        ignore_errors=True,
    )


def test_drop_retries_via_kill_connections(mocker):
    psql = mocker.patch("pgclone.db.psql", autospec=True)
    database = {"NAME": "postgres__swap"}
    db.drop(database, using="default")
    psql.assert_called_once_with(
        'DROP DATABASE IF EXISTS "postgres__swap"',
        using="default",
        kill_connections=database,
    )

import contextlib

import pytest

from pgclone import exceptions, restore_cmd


def _db(name: str) -> dict[str, str]:
    return {"NAME": name}


def test_temp_db_is_scratch(mocker):
    temp_db = _db("postgres__temp")
    pre_db = _db("postgres__pre")
    post_db = _db("postgres__post")
    mocker.patch("pgclone.restore_cmd.db.make", side_effect=lambda name, using: _db(name))

    assert restore_cmd._temp_db_is_scratch(
        "dev/default/none/dump.dump",
        temp_db=temp_db,
        pre_db=pre_db,
        post_db=post_db,
        using="default",
    )
    assert restore_cmd._temp_db_is_scratch(
        ":post", temp_db=temp_db, pre_db=pre_db, post_db=post_db, using="default"
    )
    assert restore_cmd._temp_db_is_scratch(
        ":pre", temp_db=temp_db, pre_db=pre_db, post_db=post_db, using="default"
    )
    assert restore_cmd._temp_db_is_scratch(
        ":other", temp_db=temp_db, pre_db=pre_db, post_db=post_db, using="default"
    )
    assert not restore_cmd._temp_db_is_scratch(
        ":postgres__temp", temp_db=temp_db, pre_db=pre_db, post_db=post_db, using="default"
    )


def test_restore_swapped_aside_db(mocker):
    psql = mocker.patch("pgclone.restore_cmd.db.psql")
    restore_db = _db("postgres")
    swap_db = _db("postgres__swap")

    mocker.patch("pgclone.restore_cmd._db_exists", return_value=True)
    restore_cmd._restore_swapped_aside_db(restore_db, swap_db, using="default")
    psql.assert_not_called()

    mocker.patch("pgclone.restore_cmd._db_exists", return_value=False)
    restore_cmd._restore_swapped_aside_db(restore_db, swap_db, using="default")
    psql.assert_not_called()

    mocker.patch(
        "pgclone.restore_cmd._db_exists",
        side_effect=lambda database, using: database["NAME"] == "postgres__swap",
    )
    restore_cmd._restore_swapped_aside_db(restore_db, swap_db, using="default")
    psql.assert_called_once_with(
        'ALTER DATABASE "postgres__swap" RENAME TO "postgres"',
        using="default",
        kill_connections=swap_db,
    )


def test_cleanup_drops_temp_and_post_and_puts_swap_back(mocker):
    psql = mocker.patch("pgclone.restore_cmd.db.psql")
    drop = mocker.patch("pgclone.restore_cmd.db.drop")
    mocker.patch(
        "pgclone.restore_cmd._db_exists",
        side_effect=lambda database, using: database["NAME"] != "postgres",
    )

    restore_cmd._cleanup_failed_restore(
        restore_db=_db("postgres"),
        temp_db=_db("postgres__temp"),
        swap_db=_db("postgres__swap"),
        post_db=_db("postgres__post"),
        using="default",
        swapped=False,
        created_post=True,
        drop_temp=True,
        drop_swap=False,
    )

    psql.assert_called_once()
    assert drop.call_args_list == [
        mocker.call(_db("postgres__temp"), using="default"),
        mocker.call(_db("postgres__post"), using="default"),
    ]


def test_cleanup_skips_databases_it_must_keep(mocker):
    drop = mocker.patch("pgclone.restore_cmd.db.drop")
    mocker.patch("pgclone.restore_cmd._db_exists", return_value=True)

    restore_cmd._cleanup_failed_restore(
        restore_db=_db("postgres"),
        temp_db=_db("postgres__temp"),
        swap_db=_db("postgres__swap"),
        post_db=_db("postgres__post"),
        using="default",
        swapped=False,
        created_post=False,
        drop_temp=False,
        drop_swap=False,
    )
    restore_cmd._cleanup_failed_restore(
        restore_db=_db("postgres"),
        temp_db=_db("postgres__temp"),
        swap_db=_db("postgres__swap"),
        post_db=_db("postgres__post"),
        using="default",
        swapped=True,
        created_post=True,
        drop_temp=True,
        drop_swap=False,
    )

    drop.assert_not_called()


def test_cleanup_retries_swap_drop_after_successful_swap(mocker):
    drop = mocker.patch("pgclone.restore_cmd.db.drop")
    mocker.patch("pgclone.restore_cmd._db_exists", return_value=True)

    restore_cmd._cleanup_failed_restore(
        restore_db=_db("postgres"),
        temp_db=_db("postgres__temp"),
        swap_db=_db("postgres__swap"),
        post_db=_db("postgres__post"),
        using="default",
        swapped=True,
        created_post=False,
        drop_temp=True,
        drop_swap=True,
    )

    drop.assert_called_once_with(_db("postgres__swap"), using="default")


def test_cleanup_skips_scratch_databases_that_were_never_created(mocker):
    drop = mocker.patch("pgclone.restore_cmd.db.drop")
    mocker.patch("pgclone.restore_cmd._db_exists", return_value=False)

    kwargs = {
        "restore_db": _db("postgres"),
        "temp_db": _db("postgres__temp"),
        "swap_db": _db("postgres__swap"),
        "post_db": _db("postgres__post"),
        "using": "default",
        "created_post": True,
        "drop_temp": True,
        "drop_swap": True,
    }
    restore_cmd._cleanup_failed_restore(**kwargs, swapped=False)
    restore_cmd._cleanup_failed_restore(**kwargs, swapped=True)

    drop.assert_not_called()


def test_cleanup_logs_and_continues_when_drop_fails(mocker):
    logger = mocker.Mock()
    mocker.patch("pgclone.restore_cmd.logging.get_logger", return_value=logger)
    mocker.patch("pgclone.restore_cmd._db_exists", return_value=True)
    mocker.patch(
        "pgclone.restore_cmd.db.drop",
        side_effect=exceptions.RuntimeError("drop failed"),
    )

    restore_cmd._cleanup_failed_restore(
        restore_db=_db("postgres"),
        temp_db=_db("postgres__temp"),
        swap_db=_db("postgres__swap"),
        post_db=_db("postgres__post"),
        using="default",
        swapped=False,
        created_post=True,
        drop_temp=True,
        drop_swap=False,
    )

    assert logger.exception.call_count == 2


def _patch_restore(mocker, *, psql_side_effect=None, drop_side_effect=None, exists=None):
    mocker.patch("pgclone.restore_cmd.db.conf", return_value=_db("postgres"))
    mocker.patch("pgclone.restore_cmd.db.make", side_effect=lambda name, using: _db(name))
    mocker.patch("pgclone.restore_cmd.db.route", return_value=contextlib.nullcontext())
    mocker.patch("pgclone.restore_cmd._remote_restore", return_value="dev/default/none/dump.dump")
    mocker.patch("pgclone.restore_cmd._local_restore", return_value=":other")
    mocker.patch(
        "pgclone.restore_cmd._db_exists",
        side_effect=exists or (lambda database, using: True),
    )
    psql = mocker.patch("pgclone.restore_cmd.db.psql", side_effect=psql_side_effect)
    drop = mocker.patch("pgclone.restore_cmd.db.drop", side_effect=drop_side_effect)
    return psql, drop


def _run_restore(**kwargs):
    opts = {
        "dump_key": "dev/default/none/dump.dump",
        "pre_swap_hooks": [],
        "config": "none",
        "reversible": False,
        "database": "default",
        "storage_location": "/tmp",
    }
    opts.update(kwargs)
    return restore_cmd._restore(**opts)


def test_restore_drops_temp_when_swap_rename_fails(mocker):
    def psql(sql, **kwargs):
        if '__temp" RENAME TO "postgres"' in sql:
            raise exceptions.RuntimeError("Error running command.")

    _, drop = _patch_restore(mocker, psql_side_effect=psql)

    with pytest.raises(exceptions.RuntimeError, match="Error running command"):
        _run_restore()

    drop.assert_any_call(_db("postgres__temp"), using="default")
    assert mocker.call(_db("postgres__swap"), using="default") in drop.call_args_list
    assert drop.call_args_list.count(mocker.call(_db("postgres__swap"), using="default")) == 1


def test_restore_puts_swap_back_when_live_db_was_renamed(mocker):
    def exists(database, *, using):
        return database["NAME"] in {"postgres__swap", "postgres__temp"}

    def psql(sql, **kwargs):
        if '__temp" RENAME TO "postgres"' in sql:
            raise exceptions.RuntimeError("Error running command.")

    psql_mock, drop = _patch_restore(mocker, psql_side_effect=psql, exists=exists)

    with pytest.raises(exceptions.RuntimeError, match="Error running command"):
        _run_restore()

    renames_back = [
        call
        for call in psql_mock.call_args_list
        if call.args[0] == 'ALTER DATABASE "postgres__swap" RENAME TO "postgres"'
    ]
    assert len(renames_back) == 2
    drop.assert_any_call(_db("postgres__temp"), using="default")
    assert drop.call_args_list.count(mocker.call(_db("postgres__swap"), using="default")) == 1


def test_restore_drops_post_snapshot_when_hook_fails(mocker):
    _, drop = _patch_restore(mocker)
    mocker.patch(
        "pgclone.restore_cmd.run.management",
        side_effect=exceptions.RuntimeError("hook failed"),
    )

    with pytest.raises(exceptions.RuntimeError, match="hook failed"):
        _run_restore(pre_swap_hooks=["migrate"], reversible=True)

    assert drop.call_args_list.count(mocker.call(_db("postgres__temp"), using="default")) == 1
    assert drop.call_args_list.count(mocker.call(_db("postgres__post"), using="default")) == 2


def test_restore_retries_swap_drop_after_swap_succeeds(mocker):
    swap_drops = {"n": 0}

    def drop(database, *, using):
        if database["NAME"] != "postgres__swap":
            return
        swap_drops["n"] += 1
        if swap_drops["n"] == 2:
            raise exceptions.RuntimeError("drop failed")

    _patch_restore(mocker, drop_side_effect=drop)

    with pytest.raises(exceptions.RuntimeError, match="drop failed"):
        _run_restore()

    assert swap_drops["n"] == 3


def test_restore_keeps_swap_when_reversible_pre_rename_fails(mocker):
    def psql(sql, **kwargs):
        if 'RENAME TO "postgres__pre"' in sql:
            raise exceptions.RuntimeError("Error running command.")

    _, drop = _patch_restore(mocker, psql_side_effect=psql)

    with pytest.raises(exceptions.RuntimeError, match="Error running command"):
        _run_restore(reversible=True)

    assert drop.call_args_list.count(mocker.call(_db("postgres__swap"), using="default")) == 1
    assert mocker.call(_db("postgres__temp"), using="default") not in drop.call_args_list


def test_restore_retries_swap_drop_for_local_reversible_restore(mocker):
    swap_drops = {"n": 0}

    def drop(database, *, using):
        if database["NAME"] != "postgres__swap":
            return
        swap_drops["n"] += 1
        if swap_drops["n"] == 2:
            raise exceptions.RuntimeError("drop failed")

    _patch_restore(mocker, drop_side_effect=drop)

    with pytest.raises(exceptions.RuntimeError, match="drop failed"):
        _run_restore(dump_key=":other", reversible=True)

    assert swap_drops["n"] == 3


def test_restore_does_not_drop_temp_when_it_is_the_source(mocker):
    _, drop = _patch_restore(mocker)
    mocker.patch(
        "pgclone.restore_cmd._local_restore",
        side_effect=exceptions.RuntimeError("local failed"),
    )

    with pytest.raises(exceptions.RuntimeError, match="local failed"):
        _run_restore(dump_key=":postgres__temp")

    drop.assert_not_called()


def test_restore_original_error_survives_cleanup_failure(mocker):
    logger = mocker.Mock()
    mocker.patch("pgclone.restore_cmd.logging.get_logger", return_value=logger)

    def psql(sql, **kwargs):
        if '__temp" RENAME TO "postgres"' in sql:
            raise exceptions.RuntimeError("Error running command.")

    def drop(database, *, using):
        if database["NAME"] == "postgres__temp":
            raise exceptions.RuntimeError("cleanup failed")

    _patch_restore(mocker, psql_side_effect=psql, drop_side_effect=drop)

    with pytest.raises(exceptions.RuntimeError, match="Error running command"):
        _run_restore()

    logger.exception.assert_called()

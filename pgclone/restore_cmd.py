from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from django.db import connections

from pgclone import db, exceptions, logging, ls_cmd, options, run, settings, storage


def _db_exists(database: dict[str, Any], *, using: str) -> bool:
    """Returns True if the database exists"""
    conn_db_url = db.url(db.conn(using=using))
    try:
        run.shell(f"psql {conn_db_url} -lqt | cut -d \\| -f 1 | grep -qw {database['NAME']}")
        return True
    except RuntimeError:
        return False


def _set_search_path(database: dict[str, Any], *, using: str) -> None:
    """
    pg_restore does not restore the original search_path variable of the
    database, which can cause issues with extensions. This function obtains
    the original search path and sets it as the search path of the restored
    database

    https://www.postgresql.org/message-id/CAKFQuwbWpd1DXA_YY3zXRKGKKLL1ZTi9MHDQ15zJ8ufVBwVJEA%40mail.gmail.com
    """
    with connections[using].cursor() as cursor:
        cursor.execute("SHOW search_path;")
        search_path = cursor.fetchone()
        if search_path is None:
            raise AssertionError
        search_path = search_path[0]

    set_search_path_sql = f'ALTER DATABASE "{database["NAME"]}" SET search_path to {search_path}'
    db.psql(set_search_path_sql, using=using)


def _temp_db_is_scratch(
    dump_key: str,
    *,
    temp_db: dict[str, Any],
    pre_db: dict[str, Any],
    post_db: dict[str, Any],
    using: str,
) -> bool:
    """True when __temp is a copy we created and can drop if restore fails.

    Restoring the __temp database itself uses that database as the source,
    so a failed restore must not drop it.
    """
    if not dump_key.startswith(":"):
        return True
    if dump_key == ":post":
        source = post_db
    elif dump_key == ":pre":
        source = pre_db
    else:
        source = db.make(dump_key[1:], using=using)
    return source != temp_db


def _restore_swapped_aside_db(
    restore_db: dict[str, Any],
    swap_db: dict[str, Any],
    *,
    using: str,
) -> None:
    """Put the live database back when a swap renamed it to __swap and then died.

    __swap is the previous live database in that window. Dropping it would
    delete the database that is still in use.
    """
    if _db_exists(restore_db, using=using) or not _db_exists(swap_db, using=using):
        return

    logging.success_msg(
        f'Putting "{swap_db["NAME"]}" back as "{restore_db["NAME"]}" after a failed swap'
    )
    db.psql(
        f'ALTER DATABASE "{swap_db["NAME"]}" RENAME TO "{restore_db["NAME"]}"',
        using=using,
        kill_connections=swap_db,
    )


def _best_effort(action: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:
        logging.get_logger().exception("Failed to %s after a failed restore", action)


def _cleanup_failed_restore(
    *,
    restore_db: dict[str, Any],
    temp_db: dict[str, Any],
    swap_db: dict[str, Any],
    post_db: dict[str, Any],
    using: str,
    swapped: bool,
    created_post: bool,
    drop_temp: bool,
    drop_swap: bool,
) -> None:
    """Drop scratch databases left behind when restore fails.

    __temp holds the restored copy until the swap finishes. If we stop before
    that rename, it is unused and must be dropped.

    __swap is not the same kind of scratch database. Before the swap finishes
    it may be the live database renamed aside, and we put that name back.
    After a successful swap, non-reversible restores already mean to delete
    it; retry that drop if it did not happen. Reversible restores keep it
    until it is renamed to __pre, so a failed rename leaves it in place.
    """
    if not swapped:
        _best_effort(
            "put the original database back",
            lambda: _restore_swapped_aside_db(restore_db, swap_db, using=using),
        )
        if drop_temp and _db_exists(temp_db, using=using):
            logging.success_msg(f'Dropping unused database "{temp_db["NAME"]}"')
            _best_effort("drop the temporary database", lambda: db.drop(temp_db, using=using))
        if created_post and _db_exists(post_db, using=using):
            logging.success_msg(f'Dropping unused database "{post_db["NAME"]}"')
            _best_effort("drop the post snapshot", lambda: db.drop(post_db, using=using))
        return

    if drop_swap and _db_exists(swap_db, using=using):
        logging.success_msg(f'Dropping unused database "{swap_db["NAME"]}"')
        _best_effort("drop the swap database", lambda: db.drop(swap_db, using=using))


def _local_restore(
    dump_key: str,
    *,
    temp_db: dict[str, Any],
    post_db: dict[str, Any],
    pre_db: dict[str, Any],
    using: str,
) -> str:
    """
    Performs a restore using a local database
    """
    if dump_key == ":post":
        local_restore_db = post_db
    elif dump_key == ":pre":
        local_restore_db = pre_db
    else:
        local_restore_db = db.make(dump_key[1:], using=using)

    if not _db_exists(local_restore_db, using=using):
        raise exceptions.RuntimeError(
            f'Local database "{local_restore_db["NAME"]}" does not exist.'
            ' Use "pgclone ls --local" to see local database keys.'
        )

    # Perform the local restore process. It is completely valid to
    # try to restore the temp_db, so do nothing if this database
    # is provided by the user
    if local_restore_db != temp_db:  # pragma: no branch
        db.drop(temp_db, using=using)
        create_temp_sql = (
            f'CREATE DATABASE "{temp_db["NAME"]}" WITH TEMPLATE "{local_restore_db["NAME"]}"'
        )
        db.psql(create_temp_sql, using=using)

    db.disable_autovacuum(temp_db, using=using)
    _set_search_path(temp_db, using=using)

    return dump_key


def _remote_restore(
    dump_key: str,
    *,
    temp_db: dict[str, Any],
    using: str,
    storage_location: str,
) -> str:
    storage_client = storage.client(storage_location)

    # We are restoring from a remote dump. If the dump key is not valid,
    # assume it is the latest dump of a database name and get the latest
    # dump key
    if not dump_key.endswith(".dump"):
        dump_keys = ls_cmd.ls(dump_key=dump_key, storage_location=storage_location)
        found_dump_key = dump_keys[0] if dump_keys else None

        if not found_dump_key:
            raise exceptions.RuntimeError(
                f'Could not find a dump key matching prefix "{dump_key}"'
            )

        dump_key = found_dump_key

    file_path = os.path.join(storage_location, dump_key)

    logging.success_msg("Creating the temporary restore db")
    db.drop(temp_db, using=using)
    create_temp_sql = f'CREATE DATABASE "{temp_db["NAME"]}"'
    db.psql(create_temp_sql, using=using)
    db.disable_autovacuum(temp_db, using=using)
    _set_search_path(temp_db, using=using)

    logging.success_msg(f'Running pg_restore on "{dump_key}"')
    pg_restore_cmd = f"pg_restore --verbose --no-acl --no-owner -d {db.url(temp_db)}"

    # When restoring, we need to ignore errors because there are certain
    # errors we cannot get around when pg restoring some DBs (like Aurora).
    # In the future, we may parse the output of the pg_restore command to see
    # if an unexpected error happened.
    storage_client.run_pg_restore(pg_restore_cmd, file_path)

    return dump_key


def _restore(
    *,
    dump_key: str | None,
    pre_swap_hooks: list[str],
    config: str,
    reversible: bool,
    database: str,
    storage_location: str,
) -> str:
    """
    Restore implementation
    """
    if not settings.allow_restore():  # pragma: no cover
        raise exceptions.RuntimeError("Restore not allowed.")

    if not dump_key:
        raise exceptions.ValueError("Must provide a dump key or prefix to restore.")

    # Restore works in the following steps with the following databases:
    # 1. Create the temp_db database to perform the restore without
    #    affecting the restore_db
    # 2. Call pg_restore on temp_db
    # 3. If using --reversible, create the post_db restore copy of temp_db
    # 4. Apply any pre_swap_hooks to temp_db
    # 5. Terminate all connections on restore_db so that we
    #    can swap in the temp_db
    # 6. Rename restore_db to swap_db
    # 7. Rename temp_db to restore_db
    # 8. Delete swap_db and post/pre_db if not using --reversible OR
    #    rename swap_db to pre_db if using --reversible
    #
    # If anything fails before step 7, __temp (and a __post copy created in
    # this run) is dropped. If step 6 succeeded, __swap is renamed back to
    # the live database. It is not dropped: that database is still in use.
    #
    # Database variable names below reflect this process.

    restore_db = db.conf(using=database)
    # The DB in which a restore happens behind the scenes. Pre-swap hooks
    # are applied to it
    temp_db = db.make(restore_db["NAME"] + "__temp", using=database)
    # A temporary DB to use for the swap step. Helps us do a quick rename of
    # DBs.
    swap_db = db.make(restore_db["NAME"] + "__swap", using=database)
    # These two DBs are only for reversible restores
    pre_db = db.make(restore_db["NAME"] + "__pre", using=database)
    post_db = db.make(restore_db["NAME"] + "__post", using=database)
    is_local_restore = dump_key.startswith(":")
    drop_temp = _temp_db_is_scratch(
        dump_key, temp_db=temp_db, pre_db=pre_db, post_db=post_db, using=database
    )
    swapped = False
    created_post = False

    try:
        if is_local_restore:
            dump_key = _local_restore(
                dump_key,
                temp_db=temp_db,
                post_db=post_db,
                pre_db=pre_db,
                using=database,
            )
        else:
            dump_key = _remote_restore(
                dump_key, temp_db=temp_db, using=database, storage_location=storage_location
            )

        # When in reversible mode, make a special __post db snapshot.
        # Note that reversible mode is a noop for local restores.
        if reversible and not is_local_restore:
            logging.success_msg("Creating 'post' snapshot for reversible restore")
            db.drop(post_db, using=database)
            create_post_db_sql = (
                f'CREATE DATABASE "{post_db["NAME"]}" WITH TEMPLATE "{temp_db["NAME"]}"'
            )
            db.psql(create_post_db_sql, using=database)
            created_post = True

        # pre-swap hook step
        with db.route(temp_db):
            for management_command_name in pre_swap_hooks:
                logging.success_msg(f'Running "manage.py {management_command_name}" pre_swap hook')
                run.management(management_command_name)

        # swap step
        logging.success_msg("Swapping the restored copy with the primary database")
        # A previous run may have renamed the live DB to __swap and died.
        # Put it back before dropping __swap, or this drop deletes it.
        _restore_swapped_aside_db(restore_db, swap_db, using=database)
        db.drop(swap_db, using=database)
        alter_db_sql = f'ALTER DATABASE "{restore_db["NAME"]}" RENAME TO "{swap_db["NAME"]}"'
        # There's a scenario where the restore DB may not exist before running
        # this, so just ignore errors on this command
        db.psql(alter_db_sql, ignore_errors=True, using=database, kill_connections=restore_db)

        rename_sql = f'ALTER DATABASE "{temp_db["NAME"]}" RENAME TO "{restore_db["NAME"]}"'
        db.psql(rename_sql, using=database, kill_connections=temp_db)
        swapped = True

        # If we're doing a reversible remote restore, keep the swap DB around as the prev DB
        if reversible and not is_local_restore:
            logging.success_msg("Creating 'pre' snapshot for reversible restore")
            db.drop(pre_db, using=database)
            rename_sql = f'ALTER DATABASE "{swap_db["NAME"]}" RENAME TO "{pre_db["NAME"]}"'
            db.psql(rename_sql, using=database, kill_connections=swap_db)
        else:
            logging.success_msg("Cleaning pgclone resources")
            db.drop(swap_db, using=database)
            if not reversible and not is_local_restore:
                # If we did a non-reversible remote restore, remove any previous restore
                # points from reversible restores
                db.drop(post_db, using=database)
                db.drop(pre_db, using=database)
    except Exception:
        _cleanup_failed_restore(
            restore_db=restore_db,
            temp_db=temp_db,
            swap_db=swap_db,
            post_db=post_db,
            using=database,
            swapped=swapped,
            created_post=created_post,
            drop_temp=drop_temp,
            drop_swap=swapped and not (reversible and not is_local_restore),
        )
        raise

    logging.success_msg(f'Successfully restored dump "{dump_key}" to database "{database}"')

    return dump_key


def restore(
    dump_key: str | None = None,
    *,
    pre_swap_hooks: list[str] | None = None,
    reversible: bool | None = None,
    database: str | None = None,
    storage_location: str | None = None,
    config: str | None = None,
) -> str:
    """
    Restores a database dump.

    Args:
        dump_key: Restores the specific dump key or the most recent dump matching the prefix.
        pre_swap_hooks: The list of pre-swap hooks to run before swapping the temp restore
            database with the main utils. The strings are management command names.
        reversible: True if the dump can be reversed.
        database: The database to restore.
        storage_location: The storage location to use for the restore.
        config: The configuration name from `settings.PGCLONE_CONFIGS`.

    Returns:
        The dump key that was restored.
    """
    opts = options.get(
        dump_key=dump_key,
        pre_swap_hooks=pre_swap_hooks,
        config=config,
        reversible=reversible,
        database=database,
        storage_location=storage_location,
    )

    return _restore(
        dump_key=opts.dump_key,
        pre_swap_hooks=opts.pre_swap_hooks,
        config=opts.config,
        reversible=opts.reversible,
        database=opts.database,
        storage_location=opts.storage_location,
    )

import contextlib

from sqlalchemy import Column
from sqlalchemy import event
from sqlalchemy import exc as sa_exc
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import testing
from sqlalchemy.future import Engine
from sqlalchemy.orm import attributes
from sqlalchemy.orm import create_session
from sqlalchemy.orm import exc as orm_exc
from sqlalchemy.orm import mapper
from sqlalchemy.orm import relationship
from sqlalchemy.orm import Session
from sqlalchemy.orm import session as _session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.util import identity_key
from sqlalchemy.sql import elements
from sqlalchemy.testing import assert_raises
from sqlalchemy.testing import assert_raises_message
from sqlalchemy.testing import assert_warnings
from sqlalchemy.testing import engines
from sqlalchemy.testing import eq_
from sqlalchemy.testing import expect_warnings
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_
from sqlalchemy.testing import is_not_
from sqlalchemy.testing import mock
from sqlalchemy.testing.util import gc_collect
from test.orm._fixtures import FixtureTest


class SessionTransactionTest(fixtures.RemovesEvents, FixtureTest):
    run_inserts = None
    __backend__ = True

    def test_no_close_transaction_on_flush(self):
        User, users = self.classes.User, self.tables.users

        c = testing.db.connect()
        try:
            mapper(User, users)
            s = create_session(bind=c)
            s.begin()
            tran = s.transaction
            s.add(User(name="first"))
            s.flush()
            c.exec_driver_sql("select * from users")
            u = User(name="two")
            s.add(u)
            s.flush()
            u = User(name="third")
            s.add(u)
            s.flush()
            assert s.transaction is tran
            tran.close()
        finally:
            c.close()

    @engines.close_open_connections
    def test_subtransaction_on_external_subtrans(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        conn = testing.db.connect()
        trans = conn.begin()
        sess = create_session(bind=conn, autocommit=False, autoflush=True)
        sess.begin(subtransactions=True)
        u = User(name="ed")
        sess.add(u)
        sess.flush()
        sess.commit()  # commit does nothing
        trans.rollback()  # rolls back
        assert len(sess.query(User).all()) == 0
        sess.close()

    @engines.close_open_connections
    def test_subtransaction_on_external_no_begin(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        conn = testing.db.connect()
        trans = conn.begin()
        sess = create_session(bind=conn, autocommit=False, autoflush=True)
        u = User(name="ed")
        sess.add(u)
        sess.flush()
        sess.commit()  # commit does nothing
        trans.rollback()  # rolls back
        assert len(sess.query(User).all()) == 0
        sess.close()

    @testing.requires.savepoints
    @engines.close_open_connections
    def test_external_nested_transaction(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        try:
            conn = testing.db.connect()
            trans = conn.begin()
            sess = create_session(bind=conn, autocommit=False, autoflush=True)
            u1 = User(name="u1")
            sess.add(u1)
            sess.flush()

            savepoint = sess.begin_nested()
            u2 = User(name="u2")
            sess.add(u2)
            sess.flush()
            savepoint.rollback()

            trans.commit()
            assert len(sess.query(User).all()) == 1
        except Exception:
            conn.close()
            raise

    @engines.close_open_connections
    def test_subtransaction_on_external_commit_future(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        engine = Engine._future_facade(testing.db)

        conn = engine.connect()
        conn.begin()

        sess = create_session(bind=conn, autocommit=False, autoflush=True)
        u = User(name="ed")
        sess.add(u)
        sess.flush()
        sess.commit()  # commit does nothing
        conn.rollback()  # rolls back
        assert len(sess.query(User).all()) == 0
        sess.close()

    @engines.close_open_connections
    def test_subtransaction_on_external_rollback_future(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        engine = Engine._future_facade(testing.db)

        conn = engine.connect()
        conn.begin()

        sess = create_session(bind=conn, autocommit=False, autoflush=True)
        u = User(name="ed")
        sess.add(u)
        sess.flush()
        sess.rollback()  # rolls back
        conn.commit()  # nothing to commit
        assert len(sess.query(User).all()) == 0
        sess.close()

    @testing.requires.savepoints
    @engines.close_open_connections
    def test_savepoint_on_external_future(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        engine = Engine._future_facade(testing.db)

        with engine.connect() as conn:
            conn.begin()
            sess = create_session(bind=conn, autocommit=False, autoflush=True)
            u1 = User(name="u1")
            sess.add(u1)
            sess.flush()

            sess.begin_nested()
            u2 = User(name="u2")
            sess.add(u2)
            sess.flush()
            sess.rollback()

            conn.commit()
            assert len(sess.query(User).all()) == 1

    @testing.requires.savepoints
    def test_nested_accounting_new_items_removed(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)

        session = create_session(bind=testing.db)
        session.begin()
        session.begin_nested()
        u1 = User(name="u1")
        session.add(u1)
        session.commit()
        assert u1 in session
        session.rollback()
        assert u1 not in session

    @testing.requires.savepoints
    def test_nested_accounting_deleted_items_restored(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)

        session = create_session(bind=testing.db)
        session.begin()
        u1 = User(name="u1")
        session.add(u1)
        session.commit()

        session.begin()
        u1 = session.query(User).first()

        session.begin_nested()
        session.delete(u1)
        session.commit()
        assert u1 not in session
        session.rollback()
        assert u1 in session

    @testing.requires.savepoints
    def test_heavy_nesting(self):
        users = self.tables.users

        session = create_session(bind=testing.db)
        session.begin()
        session.connection().execute(users.insert().values(name="user1"))
        session.begin(subtransactions=True)
        session.begin_nested()
        session.connection().execute(users.insert().values(name="user2"))
        assert (
            session.connection()
            .exec_driver_sql("select count(1) from users")
            .scalar()
            == 2
        )
        session.rollback()
        assert (
            session.connection()
            .exec_driver_sql("select count(1) from users")
            .scalar()
            == 1
        )
        session.connection().execute(users.insert().values(name="user3"))
        session.commit()
        assert (
            session.connection()
            .exec_driver_sql("select count(1) from users")
            .scalar()
            == 2
        )

    @testing.requires.savepoints
    def test_heavy_nesting_future(self):
        users = self.tables.users

        engine = Engine._future_facade(testing.db)
        session = create_session(engine, autocommit=False)

        session.begin()
        session.connection().execute(users.insert().values(name="user1"))
        session.begin(subtransactions=True)
        session.begin_nested()
        session.connection().execute(users.insert().values(name="user2"))
        assert (
            session.connection()
            .exec_driver_sql("select count(1) from users")
            .scalar()
            == 2
        )
        session.rollback()
        assert (
            session.connection()
            .exec_driver_sql("select count(1) from users")
            .scalar()
            == 1
        )
        session.connection().execute(users.insert().values(name="user3"))
        session.commit()
        assert (
            session.connection()
            .exec_driver_sql("select count(1) from users")
            .scalar()
            == 2
        )

    @testing.requires.savepoints
    def test_dirty_state_transferred_deep_nesting(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)

        s = Session(testing.db)
        u1 = User(name="u1")
        s.add(u1)
        s.commit()

        nt1 = s.begin_nested()
        nt2 = s.begin_nested()
        u1.name = "u2"
        assert attributes.instance_state(u1) not in nt2._dirty
        assert attributes.instance_state(u1) not in nt1._dirty
        s.flush()
        assert attributes.instance_state(u1) in nt2._dirty
        assert attributes.instance_state(u1) not in nt1._dirty

        s.commit()
        assert attributes.instance_state(u1) in nt2._dirty
        assert attributes.instance_state(u1) in nt1._dirty

        s.rollback()
        assert attributes.instance_state(u1).expired
        eq_(u1.name, "u1")

    @testing.requires.savepoints
    def test_dirty_state_transferred_deep_nesting_future(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)

        s = Session(testing.db, future=True)
        u1 = User(name="u1")
        s.add(u1)
        s.commit()

        nt1 = s.begin_nested()
        nt2 = s.begin_nested()
        u1.name = "u2"
        assert attributes.instance_state(u1) not in nt2._dirty
        assert attributes.instance_state(u1) not in nt1._dirty
        s.flush()
        assert attributes.instance_state(u1) in nt2._dirty
        assert attributes.instance_state(u1) not in nt1._dirty

        nt2.commit()
        assert attributes.instance_state(u1) in nt2._dirty
        assert attributes.instance_state(u1) in nt1._dirty

        nt1.rollback()
        assert attributes.instance_state(u1).expired
        eq_(u1.name, "u1")

    @testing.requires.independent_connections
    def test_transactions_isolated(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)

        s1 = create_session(bind=testing.db, autocommit=False)
        s2 = create_session(bind=testing.db, autocommit=False)
        u1 = User(name="u1")
        s1.add(u1)
        s1.flush()

        assert s2.query(User).all() == []

    @testing.requires.two_phase_transactions
    def test_twophase(self):
        users, Address, addresses, User = (
            self.tables.users,
            self.classes.Address,
            self.tables.addresses,
            self.classes.User,
        )

        # TODO: mock up a failure condition here
        # to ensure a rollback succeeds
        mapper(User, users)
        mapper(Address, addresses)

        engine2 = engines.testing_engine()
        sess = create_session(autocommit=True, autoflush=False, twophase=True)
        sess.bind_mapper(User, testing.db)
        sess.bind_mapper(Address, engine2)
        sess.begin()
        u1 = User(name="u1")
        a1 = Address(email_address="u1@e")
        sess.add_all((u1, a1))
        sess.commit()
        sess.close()
        engine2.dispose()
        eq_(select(func.count("*")).select_from(users).scalar(), 1)
        eq_(select(func.count("*")).select_from(addresses).scalar(), 1)

    @testing.requires.independent_connections
    def test_invalidate(self):
        User, users = self.classes.User, self.tables.users
        mapper(User, users)
        sess = Session()
        u = User(name="u1")
        sess.add(u)
        sess.flush()
        c1 = sess.connection(bind_arguments={"mapper": User})
        dbapi_conn = c1.connection
        assert dbapi_conn.is_valid

        sess.invalidate()

        # Connection object is closed
        assert c1.closed

        # "invalidated" is not part of "closed" state
        assert not c1.invalidated

        # but the DBAPI conn (really ConnectionFairy)
        # is invalidated
        assert not dbapi_conn.is_valid

        eq_(sess.query(User).all(), [])
        c2 = sess.connection(bind_arguments={"mapper": User})
        assert not c2.invalidated
        assert c2.connection.is_valid

    def test_subtransaction_on_noautocommit(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)
        sess = create_session(autocommit=False, autoflush=True)
        sess.begin(subtransactions=True)
        u = User(name="u1")
        sess.add(u)
        sess.flush()
        sess.commit()  # commit does nothing
        sess.rollback()  # rolls back
        assert len(sess.query(User).all()) == 0
        sess.close()

    @testing.requires.savepoints
    def test_nested_transaction(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)
        sess = create_session()
        sess.begin()

        u = User(name="u1")
        sess.add(u)
        sess.flush()

        sess.begin_nested()  # nested transaction

        u2 = User(name="u2")
        sess.add(u2)
        sess.flush()

        sess.rollback()

        sess.commit()
        assert len(sess.query(User).all()) == 1
        sess.close()

    @testing.requires.savepoints
    def test_nested_autotrans(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)
        sess = create_session(autocommit=False)
        u = User(name="u1")
        sess.add(u)
        sess.flush()

        sess.begin_nested()  # nested transaction

        u2 = User(name="u2")
        sess.add(u2)
        sess.flush()

        sess.rollback()  # rolls back nested only

        sess.commit()
        assert len(sess.query(User).all()) == 1
        sess.close()

    @testing.requires.savepoints
    def test_nested_autotrans_future(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)
        sess = create_session(autocommit=False, future=True)
        u = User(name="u1")
        sess.add(u)
        sess.flush()

        sess.begin_nested()  # nested transaction

        u2 = User(name="u2")
        sess.add(u2)
        sess.flush()

        sess.rollback()  # rolls back the whole trans

        sess.commit()
        assert len(sess.query(User).all()) == 0
        sess.close()

    @testing.requires.savepoints
    def test_nested_transaction_connection_add(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        sess = create_session(autocommit=True)

        sess.begin()
        sess.begin_nested()

        u1 = User(name="u1")
        sess.add(u1)
        sess.flush()

        sess.rollback()

        u2 = User(name="u2")
        sess.add(u2)

        sess.commit()

        eq_(set(sess.query(User).all()), set([u2]))

        sess.begin()
        sess.begin_nested()

        u3 = User(name="u3")
        sess.add(u3)
        sess.commit()  # commit the nested transaction
        sess.rollback()

        eq_(set(sess.query(User).all()), set([u2]))

        sess.close()

    @testing.requires.savepoints
    def test_mixed_transaction_control(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        sess = create_session(autocommit=True)

        sess.begin()
        sess.begin_nested()
        transaction = sess.begin(subtransactions=True)

        sess.add(User(name="u1"))

        transaction.commit()
        sess.commit()
        sess.commit()

        sess.close()

        eq_(len(sess.query(User).all()), 1)

        t1 = sess.begin()
        t2 = sess.begin_nested()

        sess.add(User(name="u2"))

        t2.commit()
        assert sess.transaction is t1

        sess.close()

    @testing.requires.savepoints
    def test_mixed_transaction_close(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        sess = create_session(autocommit=False)

        sess.begin_nested()

        sess.add(User(name="u1"))
        sess.flush()

        sess.close()

        sess.add(User(name="u2"))
        sess.commit()

        sess.close()

        eq_(len(sess.query(User).all()), 1)

    def test_begin_fails_connection_is_closed(self):
        eng = engines.testing_engine()

        state = []

        @event.listens_for(eng, "begin")
        def do_begin(conn):
            state.append((conn, conn.connection))
            raise Exception("failure")

        s1 = Session(eng)

        assert_raises_message(Exception, "failure", s1.execute, "select 1")

        conn, fairy = state[0]
        assert not fairy.is_valid
        assert conn.closed
        assert not conn.invalidated

        s1.close()

        # close does not occur because references were not saved, however
        # the underlying DBAPI connection was closed
        assert not fairy.is_valid
        assert conn.closed
        assert not conn.invalidated

    def test_begin_savepoint_fails_connection_is_not_closed(self):
        eng = engines.testing_engine()

        state = []

        @event.listens_for(eng, "savepoint")
        def do_begin(conn, name):
            state.append((conn, conn.connection))
            raise Exception("failure")

        s1 = Session(eng)

        s1.begin_nested()
        assert_raises_message(Exception, "failure", s1.execute, "select 1")

        conn, fairy = state[0]
        assert fairy.is_valid
        assert not conn.closed
        assert not conn.invalidated

        s1.close()

        assert conn.closed
        assert not fairy.is_valid

    def test_continue_flushing_on_commit(self):
        """test that post-flush actions get flushed also if
        we're in commit()"""
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        sess = Session()

        to_flush = [User(name="ed"), User(name="jack"), User(name="wendy")]

        @event.listens_for(sess, "after_flush_postexec")
        def add_another_user(session, ctx):
            if to_flush:
                session.add(to_flush.pop(0))

        x = [1]

        @event.listens_for(sess, "after_commit")  # noqa
        def add_another_user(session):  # noqa
            x[0] += 1

        sess.add(to_flush.pop())
        sess.commit()
        eq_(x, [2])
        eq_(sess.scalar(select(func.count(users.c.id))), 3)

    def test_continue_flushing_guard(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        sess = Session()

        @event.listens_for(sess, "after_flush_postexec")
        def add_another_user(session, ctx):
            session.add(User(name="x"))

        sess.add(User(name="x"))
        assert_raises_message(
            orm_exc.FlushError,
            "Over 100 subsequent flushes have occurred",
            sess.commit,
        )

    def test_error_on_using_inactive_session_commands(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        sess = create_session(autocommit=True)
        sess.begin()
        sess.begin(subtransactions=True)
        sess.add(User(name="u1"))
        sess.flush()
        sess.rollback()
        assert_raises_message(
            sa_exc.InvalidRequestError,
            "This session is in 'inactive' state, due to the SQL transaction "
            "being rolled back; no further SQL can be emitted within this "
            "transaction.",
            sess.begin,
            subtransactions=True,
        )
        sess.close()

    def test_no_sql_during_commit(self):
        sess = create_session(bind=testing.db, autocommit=False)

        @event.listens_for(sess, "after_commit")
        def go(session):
            session.execute("select 1")

        assert_raises_message(
            sa_exc.InvalidRequestError,
            "This session is in 'committed' state; no further "
            "SQL can be emitted within this transaction.",
            sess.commit,
        )

    def test_no_sql_during_prepare(self):
        sess = create_session(bind=testing.db, autocommit=False, twophase=True)

        sess.prepare()

        assert_raises_message(
            sa_exc.InvalidRequestError,
            "This session is in 'prepared' state; no further "
            "SQL can be emitted within this transaction.",
            sess.execute,
            "select 1",
        )

    def test_no_sql_during_rollback(self):
        sess = create_session(bind=testing.db, autocommit=False)

        sess.connection()

        @event.listens_for(sess, "after_rollback")
        def go(session):
            session.execute("select 1")

        assert_raises_message(
            sa_exc.InvalidRequestError,
            "This session is in 'inactive' state, due to the SQL transaction "
            "being rolled back; no further SQL can be emitted within this "
            "transaction.",
            sess.rollback,
        )

    @testing.requires.independent_connections
    @testing.emits_warning(".*previous exception")
    def test_failed_rollback_deactivates_transaction(self):
        # test #4050
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        session = Session(bind=testing.db)

        rollback_error = testing.db.dialect.dbapi.InterfaceError(
            "Can't roll back to savepoint"
        )

        def prevent_savepoint_rollback(
            cursor, statement, parameters, context=None
        ):
            if (
                context is not None
                and context.compiled
                and isinstance(
                    context.compiled.statement,
                    elements.RollbackToSavepointClause,
                )
            ):
                raise rollback_error

        self.event_listen(
            testing.db.dialect, "do_execute", prevent_savepoint_rollback
        )

        with session.begin():
            session.add(User(id=1, name="x"))

        session.begin_nested()
        # raises IntegrityError on flush
        session.add(User(id=1, name="x"))
        assert_raises_message(
            sa_exc.InterfaceError,
            "Can't roll back to savepoint",
            session.commit,
        )

        # rollback succeeds, because the Session is deactivated
        eq_(session._transaction._state, _session.DEACTIVE)
        eq_(session.is_active, False)
        session.rollback()

        # back to normal
        eq_(session._transaction._state, _session.ACTIVE)
        eq_(session.is_active, True)

        trans = session._transaction

        # leave the outermost trans
        session.rollback()

        # trans is now closed
        eq_(trans._state, _session.CLOSED)

        # outermost transaction is new
        is_not_(session._transaction, trans)

        is_(session._transaction, None)
        eq_(session.is_active, True)

    def test_no_prepare_wo_twophase(self):
        sess = create_session(bind=testing.db, autocommit=False)

        assert_raises_message(
            sa_exc.InvalidRequestError,
            "'twophase' mode not enabled, or not root "
            "transaction; can't prepare.",
            sess.prepare,
        )

    def test_closed_status_check(self):
        sess = create_session()
        trans = sess.begin()
        trans.rollback()
        assert_raises_message(
            sa_exc.ResourceClosedError,
            "This transaction is closed",
            trans.rollback,
        )
        assert_raises_message(
            sa_exc.ResourceClosedError,
            "This transaction is closed",
            trans.commit,
        )

    def test_deactive_status_check(self):
        sess = create_session()
        trans = sess.begin()
        trans2 = sess.begin(subtransactions=True)
        trans2.rollback()
        assert_raises_message(
            sa_exc.InvalidRequestError,
            "This session is in 'inactive' state, due to the SQL transaction "
            "being rolled back; no further SQL can be emitted within this "
            "transaction.",
            trans.commit,
        )

    def test_deactive_status_check_w_exception(self):
        sess = create_session()
        trans = sess.begin()
        trans2 = sess.begin(subtransactions=True)
        try:
            raise Exception("test")
        except Exception:
            trans2.rollback(_capture_exception=True)
        assert_raises_message(
            sa_exc.PendingRollbackError,
            r"This Session's transaction has been rolled back due to a "
            r"previous exception during flush. To begin a new transaction "
            r"with this Session, first issue Session.rollback\(\). "
            r"Original exception was: test",
            trans.commit,
        )

    def _inactive_flushed_session_fixture(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        sess = Session()
        u1 = User(id=1, name="u1")
        sess.add(u1)
        sess.commit()

        sess.add(User(id=1, name="u2"))

        with expect_warnings("New instance"):
            assert_raises(sa_exc.IntegrityError, sess.flush)
        return sess, u1

    def test_execution_options_begin_transaction(self):
        bind = mock.Mock(
            connect=mock.Mock(
                return_value=mock.Mock(
                    _is_future=False,
                    execution_options=mock.Mock(
                        return_value=mock.Mock(_is_future=False)
                    ),
                )
            )
        )

        sess = Session(bind=bind)
        c1 = sess.connection(execution_options={"isolation_level": "FOO"})
        eq_(bind.mock_calls, [mock.call.connect()])
        eq_(
            bind.connect().mock_calls,
            [mock.call.execution_options(isolation_level="FOO")],
        )

        eq_(c1, bind.connect().execution_options())

    def test_execution_options_ignored_mid_transaction(self):
        bind = mock.Mock()
        conn = mock.Mock(engine=bind)
        bind.connect = mock.Mock(return_value=conn)
        sess = Session(bind=bind)
        sess.execute("select 1")
        with expect_warnings(
            "Connection is already established for the "
            "given bind; execution_options ignored"
        ):
            sess.connection(execution_options={"isolation_level": "FOO"})

    def test_warning_on_using_inactive_session_new(self):
        User = self.classes.User

        sess, u1 = self._inactive_flushed_session_fixture()
        u2 = User(name="u2")
        sess.add(u2)

        def go():
            sess.rollback()

        assert_warnings(
            go,
            [
                "Session's state has been changed on a "
                "non-active transaction - this state "
                "will be discarded."
            ],
        )
        assert u2 not in sess
        assert u1 in sess

    def test_warning_on_using_inactive_session_dirty(self):
        sess, u1 = self._inactive_flushed_session_fixture()
        u1.name = "newname"

        def go():
            sess.rollback()

        assert_warnings(
            go,
            [
                "Session's state has been changed on a "
                "non-active transaction - this state "
                "will be discarded."
            ],
        )
        assert u1 in sess
        assert u1 not in sess.dirty

    def test_warning_on_using_inactive_session_delete(self):
        sess, u1 = self._inactive_flushed_session_fixture()
        sess.delete(u1)

        def go():
            sess.rollback()

        assert_warnings(
            go,
            [
                "Session's state has been changed on a "
                "non-active transaction - this state "
                "will be discarded."
            ],
        )
        assert u1 in sess
        assert u1 not in sess.deleted

    def test_warning_on_using_inactive_session_rollback_evt(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        sess = Session()
        u1 = User(id=1, name="u1")
        sess.add(u1)
        sess.commit()

        u3 = User(name="u3")

        @event.listens_for(sess, "after_rollback")
        def evt(s):
            sess.add(u3)

        sess.add(User(id=1, name="u2"))

        def go():
            assert_raises(orm_exc.FlushError, sess.flush)

        assert u3 not in sess

    def test_preserve_flush_error(self):
        User = self.classes.User

        sess, u1 = self._inactive_flushed_session_fixture()

        for i in range(5):
            assert_raises_message(
                sa_exc.PendingRollbackError,
                "^This Session's transaction has been "
                r"rolled back due to a previous exception "
                "during flush. To "
                "begin a new transaction with this "
                "Session, first issue "
                r"Session.rollback\(\). Original exception "
                "was:",
                sess.commit,
            )
        sess.rollback()
        sess.add(User(id=5, name="some name"))
        sess.commit()

    def test_no_autocommit_with_explicit_commit(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)
        session = create_session(autocommit=False)
        session.add(User(name="ed"))
        session.transaction.commit()

        is_not_(session.transaction, None)

    def test_no_autocommit_with_explicit_commit_future(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)
        session = create_session(autocommit=False, future=True)
        session.add(User(name="ed"))
        session.transaction.commit()

        # new in 1.4
        is_(session.transaction, None)

    @testing.requires.python2
    @testing.requires.savepoints_w_release
    def test_report_primary_error_when_rollback_fails(self):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)

        session = Session(testing.db)

        with expect_warnings(".*during handling of a previous exception.*"):
            session.begin_nested()
            savepoint = session.connection()._nested_transaction._savepoint

            # force the savepoint to disappear
            session.connection().dialect.do_release_savepoint(
                session.connection(), savepoint
            )

            # now do a broken flush
            session.add_all([User(id=1), User(id=1)])

            assert_raises_message(
                sa_exc.DBAPIError, "ROLLBACK TO SAVEPOINT ", session.flush
            )


class _LocalFixture(FixtureTest):
    run_setup_mappers = "once"
    run_inserts = None
    session = sessionmaker()

    @classmethod
    def setup_mappers(cls):
        User, Address = cls.classes.User, cls.classes.Address
        users, addresses = cls.tables.users, cls.tables.addresses
        mapper(
            User,
            users,
            properties={
                "addresses": relationship(
                    Address,
                    backref="user",
                    cascade="all, delete-orphan",
                    order_by=addresses.c.id,
                )
            },
        )
        mapper(Address, addresses)


class SubtransactionRecipeTest(FixtureTest):
    run_inserts = None
    __backend__ = True

    future = False

    @testing.fixture
    def subtransaction_recipe(self):
        @contextlib.contextmanager
        def transaction(session):

            if session.in_transaction():
                outermost = False
            else:
                outermost = True
                session.begin()

            try:
                yield
            except:
                if session.in_transaction():
                    session.rollback()
                raise
            else:
                if outermost and session.in_transaction():
                    session.commit()

        return transaction

    @testing.requires.savepoints
    def test_recipe_heavy_nesting(self, subtransaction_recipe):
        users = self.tables.users

        session = Session(testing.db, future=self.future)

        with subtransaction_recipe(session):
            session.connection().execute(users.insert().values(name="user1"))
            with subtransaction_recipe(session):
                savepoint = session.begin_nested()
                session.connection().execute(
                    users.insert().values(name="user2")
                )
                assert (
                    session.connection()
                    .exec_driver_sql("select count(1) from users")
                    .scalar()
                    == 2
                )
                savepoint.rollback()

            with subtransaction_recipe(session):
                assert (
                    session.connection()
                    .exec_driver_sql("select count(1) from users")
                    .scalar()
                    == 1
                )
                session.connection().execute(
                    users.insert().values(name="user3")
                )
            assert (
                session.connection()
                .exec_driver_sql("select count(1) from users")
                .scalar()
                == 2
            )

    @engines.close_open_connections
    def test_recipe_subtransaction_on_external_subtrans(
        self, subtransaction_recipe
    ):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        conn = testing.db.connect()
        trans = conn.begin()
        sess = Session(conn, future=self.future)

        with subtransaction_recipe(sess):
            u = User(name="ed")
            sess.add(u)
            sess.flush()
            # commit does nothing
        trans.rollback()  # rolls back
        assert len(sess.query(User).all()) == 0
        sess.close()

    def test_recipe_commit_one(self, subtransaction_recipe):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)
        sess = Session(testing.db, future=self.future)

        with subtransaction_recipe(sess):
            u = User(name="u1")
            sess.add(u)
        sess.close()
        assert len(sess.query(User).all()) == 1

    def test_recipe_subtransaction_on_noautocommit(
        self, subtransaction_recipe
    ):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)
        sess = Session(testing.db, future=self.future)

        sess.begin()
        with subtransaction_recipe(sess):
            u = User(name="u1")
            sess.add(u)
            sess.flush()
        sess.rollback()  # rolls back
        assert len(sess.query(User).all()) == 0
        sess.close()

    @testing.requires.savepoints
    def test_recipe_mixed_transaction_control(self, subtransaction_recipe):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        sess = Session(testing.db, future=self.future)

        sess.begin()
        sess.begin_nested()

        with subtransaction_recipe(sess):

            sess.add(User(name="u1"))

        sess.commit()
        sess.commit()

        eq_(len(sess.query(User).all()), 1)
        sess.close()

        t1 = sess.begin()
        t2 = sess.begin_nested()

        sess.add(User(name="u2"))

        t2.commit()
        assert sess.transaction is t1

        sess.close()

    def test_recipe_error_on_using_inactive_session_commands(
        self, subtransaction_recipe
    ):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)
        sess = Session(testing.db, future=self.future)
        sess.begin()

        try:
            with subtransaction_recipe(sess):
                sess.add(User(name="u1"))
                sess.flush()
                raise Exception("force rollback")
        except:
            pass

        # that was a real rollback, so no transaction
        is_(sess.get_transaction(), None)

        sess.close()

    def test_recipe_multi_nesting(self, subtransaction_recipe):
        sess = Session(testing.db, future=self.future)

        with subtransaction_recipe(sess):
            assert sess.in_transaction()

            try:
                with subtransaction_recipe(sess):
                    assert sess.transaction
                    raise Exception("force rollback")
            except:
                pass

            assert not sess.in_transaction()

    def test_recipe_deactive_status_check(self, subtransaction_recipe):
        sess = Session(testing.db, future=self.future)
        sess.begin()

        with subtransaction_recipe(sess):
            sess.rollback()

        assert not sess.in_transaction()
        sess.commit()  # no error


class FutureSubtransactionRecipeTest(SubtransactionRecipeTest):
    future = True


class FixtureDataTest(_LocalFixture):
    run_inserts = "each"
    __backend__ = True

    def test_attrs_on_rollback(self):
        User = self.classes.User
        sess = self.session()
        u1 = sess.query(User).get(7)
        u1.name = "ed"
        sess.rollback()
        eq_(u1.name, "jack")

    def test_commit_persistent(self):
        User = self.classes.User
        sess = self.session()
        u1 = sess.query(User).get(7)
        u1.name = "ed"
        sess.flush()
        sess.commit()
        eq_(u1.name, "ed")

    def test_concurrent_commit_persistent(self):
        User = self.classes.User
        s1 = self.session()
        u1 = s1.query(User).get(7)
        u1.name = "ed"
        s1.commit()

        s2 = self.session()
        u2 = s2.query(User).get(7)
        assert u2.name == "ed"
        u2.name = "will"
        s2.commit()

        assert u1.name == "will"


class CleanSavepointTest(FixtureTest):

    """test the behavior for [ticket:2452] - rollback on begin_nested()
    only expires objects tracked as being modified in that transaction.

    """

    run_inserts = None
    __backend__ = True

    def _run_test(self, update_fn, future=False):
        User, users = self.classes.User, self.tables.users

        mapper(User, users)

        s = Session(bind=testing.db, future=future)
        u1 = User(name="u1")
        u2 = User(name="u2")
        s.add_all([u1, u2])
        s.commit()
        u1.name
        u2.name
        trans = s._transaction
        assert trans is not None
        s.begin_nested()
        update_fn(s, u2)
        eq_(u2.name, "u2modified")
        s.rollback()

        if future:
            assert s._transaction is None
            assert "name" not in u1.__dict__
        else:
            assert s._transaction is trans
            eq_(u1.__dict__["name"], "u1")
        assert "name" not in u2.__dict__
        eq_(u2.name, "u2")

    @testing.requires.savepoints
    def test_rollback_ignores_clean_on_savepoint(self):
        def update_fn(s, u2):
            u2.name = "u2modified"

        self._run_test(update_fn)

    @testing.requires.savepoints
    def test_rollback_ignores_clean_on_savepoint_agg_upd_eval(self):
        User = self.classes.User

        def update_fn(s, u2):
            s.query(User).filter_by(name="u2").update(
                dict(name="u2modified"), synchronize_session="evaluate"
            )

        self._run_test(update_fn)

    @testing.requires.savepoints
    def test_rollback_ignores_clean_on_savepoint_agg_upd_fetch(self):
        User = self.classes.User

        def update_fn(s, u2):
            s.query(User).filter_by(name="u2").update(
                dict(name="u2modified"), synchronize_session="fetch"
            )

        self._run_test(update_fn)


class AutoExpireTest(_LocalFixture):
    __backend__ = True

    def test_expunge_pending_on_rollback(self):
        User = self.classes.User
        sess = self.session()
        u2 = User(name="newuser")
        sess.add(u2)
        assert u2 in sess
        sess.rollback()
        assert u2 not in sess

    def test_trans_pending_cleared_on_commit(self):
        User = self.classes.User
        sess = self.session()
        u2 = User(name="newuser")
        sess.add(u2)
        assert u2 in sess
        sess.commit()
        assert u2 in sess
        u3 = User(name="anotheruser")
        sess.add(u3)
        sess.rollback()
        assert u3 not in sess
        assert u2 in sess

    def test_update_deleted_on_rollback(self):
        User = self.classes.User
        s = self.session()
        u1 = User(name="ed")
        s.add(u1)
        s.commit()

        # this actually tests that the delete() operation,
        # when cascaded to the "addresses" collection, does not
        # trigger a flush (via lazyload) before the cascade is complete.
        s.delete(u1)
        assert u1 in s.deleted
        s.rollback()
        assert u1 in s
        assert u1 not in s.deleted

    @testing.requires.predictable_gc
    def test_gced_delete_on_rollback(self):
        User, users = self.classes.User, self.tables.users

        s = self.session()
        u1 = User(name="ed")
        s.add(u1)
        s.commit()

        s.delete(u1)
        u1_state = attributes.instance_state(u1)
        assert u1_state in s.identity_map.all_states()
        assert u1_state in s._deleted
        s.flush()
        assert u1_state not in s.identity_map.all_states()
        assert u1_state not in s._deleted
        del u1
        gc_collect()
        assert u1_state.obj() is None

        s.rollback()
        # new in 1.1, not in identity map if the object was
        # gc'ed and we restore snapshot; we've changed update_impl
        # to just skip this object
        assert u1_state not in s.identity_map.all_states()

        # in any version, the state is replaced by the query
        # because the identity map would switch it
        u1 = s.query(User).filter_by(name="ed").one()
        assert u1_state not in s.identity_map.all_states()

        eq_(s.scalar(select(func.count("*")).select_from(users)), 1)
        s.delete(u1)
        s.flush()
        eq_(s.scalar(select(func.count("*")).select_from(users)), 0)
        s.commit()

    def test_trans_deleted_cleared_on_rollback(self):
        User = self.classes.User
        s = self.session()
        u1 = User(name="ed")
        s.add(u1)
        s.commit()

        s.delete(u1)
        s.commit()
        assert u1 not in s
        s.rollback()
        assert u1 not in s

    def test_update_deleted_on_rollback_cascade(self):
        User, Address = self.classes.User, self.classes.Address

        s = self.session()
        u1 = User(name="ed", addresses=[Address(email_address="foo")])
        s.add(u1)
        s.commit()

        s.delete(u1)
        assert u1 in s.deleted
        assert u1.addresses[0] in s.deleted
        s.rollback()
        assert u1 in s
        assert u1 not in s.deleted
        assert u1.addresses[0] not in s.deleted

    def test_update_deleted_on_rollback_orphan(self):
        User, Address = self.classes.User, self.classes.Address

        s = self.session()
        u1 = User(name="ed", addresses=[Address(email_address="foo")])
        s.add(u1)
        s.commit()

        a1 = u1.addresses[0]
        u1.addresses.remove(a1)

        s.flush()
        eq_(s.query(Address).filter(Address.email_address == "foo").all(), [])
        s.rollback()
        assert a1 not in s.deleted
        assert u1.addresses == [a1]

    def test_commit_pending(self):
        User = self.classes.User
        sess = self.session()
        u1 = User(name="newuser")
        sess.add(u1)
        sess.flush()
        sess.commit()
        eq_(u1.name, "newuser")

    def test_concurrent_commit_pending(self):
        User = self.classes.User
        s1 = self.session()
        u1 = User(name="edward")
        s1.add(u1)
        s1.commit()

        s2 = self.session()
        u2 = s2.query(User).filter(User.name == "edward").one()
        u2.name = "will"
        s2.commit()

        assert u1.name == "will"


class TwoPhaseTest(_LocalFixture):
    __backend__ = True

    @testing.requires.two_phase_transactions
    def test_rollback_on_prepare(self):
        User = self.classes.User
        s = self.session(twophase=True)

        u = User(name="ed")
        s.add(u)
        s.prepare()
        s.rollback()

        assert u not in s


class RollbackRecoverTest(_LocalFixture):
    __backend__ = True

    def test_pk_violation(self):
        User, Address = self.classes.User, self.classes.Address
        s = self.session()

        a1 = Address(email_address="foo")
        u1 = User(id=1, name="ed", addresses=[a1])
        s.add(u1)
        s.commit()

        a2 = Address(email_address="bar")
        u2 = User(id=1, name="jack", addresses=[a2])

        u1.name = "edward"
        a1.email_address = "foober"
        s.add(u2)

        with expect_warnings("New instance"):
            assert_raises(sa_exc.IntegrityError, s.commit)

        assert_raises(sa_exc.InvalidRequestError, s.commit)
        s.rollback()
        assert u2 not in s
        assert a2 not in s
        assert u1 in s
        assert a1 in s
        assert u1.name == "ed"
        assert a1.email_address == "foo"
        u1.name = "edward"
        a1.email_address = "foober"
        s.commit()
        eq_(
            s.query(User).all(),
            [
                User(
                    id=1,
                    name="edward",
                    addresses=[Address(email_address="foober")],
                )
            ],
        )

    @testing.requires.savepoints
    def test_pk_violation_with_savepoint(self):
        User, Address = self.classes.User, self.classes.Address
        s = self.session()
        a1 = Address(email_address="foo")
        u1 = User(id=1, name="ed", addresses=[a1])
        s.add(u1)
        s.commit()

        a2 = Address(email_address="bar")
        u2 = User(id=1, name="jack", addresses=[a2])

        u1.name = "edward"
        a1.email_address = "foober"
        s.begin_nested()
        s.add(u2)

        with expect_warnings("New instance"):
            assert_raises(sa_exc.IntegrityError, s.commit)
        assert_raises(sa_exc.InvalidRequestError, s.commit)
        s.rollback()
        assert u2 not in s
        assert a2 not in s
        assert u1 in s
        assert a1 in s

        s.commit()
        eq_(
            s.query(User).all(),
            [
                User(
                    id=1,
                    name="edward",
                    addresses=[Address(email_address="foober")],
                )
            ],
        )


class SavepointTest(_LocalFixture):
    __backend__ = True

    @testing.requires.savepoints
    def test_savepoint_rollback(self):
        User = self.classes.User
        s = self.session()
        u1 = User(name="ed")
        u2 = User(name="jack")
        s.add_all([u1, u2])

        s.begin_nested()
        u3 = User(name="wendy")
        u4 = User(name="foo")
        u1.name = "edward"
        u2.name = "jackward"
        s.add_all([u3, u4])
        eq_(
            s.query(User.name).order_by(User.id).all(),
            [("edward",), ("jackward",), ("wendy",), ("foo",)],
        )
        s.rollback()
        assert u1.name == "ed"
        assert u2.name == "jack"
        eq_(s.query(User.name).order_by(User.id).all(), [("ed",), ("jack",)])
        s.commit()
        assert u1.name == "ed"
        assert u2.name == "jack"
        eq_(s.query(User.name).order_by(User.id).all(), [("ed",), ("jack",)])

    @testing.requires.savepoints
    def test_savepoint_delete(self):
        User = self.classes.User
        s = self.session()
        u1 = User(name="ed")
        s.add(u1)
        s.commit()
        eq_(s.query(User).filter_by(name="ed").count(), 1)
        s.begin_nested()
        s.delete(u1)
        s.commit()
        eq_(s.query(User).filter_by(name="ed").count(), 0)
        s.commit()

    @testing.requires.savepoints
    def test_savepoint_commit(self):
        User = self.classes.User
        s = self.session()
        u1 = User(name="ed")
        u2 = User(name="jack")
        s.add_all([u1, u2])

        s.begin_nested()
        u3 = User(name="wendy")
        u4 = User(name="foo")
        u1.name = "edward"
        u2.name = "jackward"
        s.add_all([u3, u4])
        eq_(
            s.query(User.name).order_by(User.id).all(),
            [("edward",), ("jackward",), ("wendy",), ("foo",)],
        )
        s.commit()

        def go():
            assert u1.name == "edward"
            assert u2.name == "jackward"
            eq_(
                s.query(User.name).order_by(User.id).all(),
                [("edward",), ("jackward",), ("wendy",), ("foo",)],
            )

        self.assert_sql_count(testing.db, go, 1)

        s.commit()
        eq_(
            s.query(User.name).order_by(User.id).all(),
            [("edward",), ("jackward",), ("wendy",), ("foo",)],
        )

    @testing.requires.savepoints
    def test_savepoint_rollback_collections(self):
        User, Address = self.classes.User, self.classes.Address
        s = self.session()
        u1 = User(name="ed", addresses=[Address(email_address="foo")])
        s.add(u1)
        s.commit()

        u1.name = "edward"
        u1.addresses.append(Address(email_address="bar"))
        s.begin_nested()
        u2 = User(name="jack", addresses=[Address(email_address="bat")])
        s.add(u2)
        eq_(
            s.query(User).order_by(User.id).all(),
            [
                User(
                    name="edward",
                    addresses=[
                        Address(email_address="foo"),
                        Address(email_address="bar"),
                    ],
                ),
                User(name="jack", addresses=[Address(email_address="bat")]),
            ],
        )
        s.rollback()
        eq_(
            s.query(User).order_by(User.id).all(),
            [
                User(
                    name="edward",
                    addresses=[
                        Address(email_address="foo"),
                        Address(email_address="bar"),
                    ],
                )
            ],
        )
        s.commit()
        eq_(
            s.query(User).order_by(User.id).all(),
            [
                User(
                    name="edward",
                    addresses=[
                        Address(email_address="foo"),
                        Address(email_address="bar"),
                    ],
                )
            ],
        )

    @testing.requires.savepoints
    def test_savepoint_commit_collections(self):
        User, Address = self.classes.User, self.classes.Address
        s = self.session()
        u1 = User(name="ed", addresses=[Address(email_address="foo")])
        s.add(u1)
        s.commit()

        u1.name = "edward"
        u1.addresses.append(Address(email_address="bar"))
        s.begin_nested()
        u2 = User(name="jack", addresses=[Address(email_address="bat")])
        s.add(u2)
        eq_(
            s.query(User).order_by(User.id).all(),
            [
                User(
                    name="edward",
                    addresses=[
                        Address(email_address="foo"),
                        Address(email_address="bar"),
                    ],
                ),
                User(name="jack", addresses=[Address(email_address="bat")]),
            ],
        )
        s.commit()
        eq_(
            s.query(User).order_by(User.id).all(),
            [
                User(
                    name="edward",
                    addresses=[
                        Address(email_address="foo"),
                        Address(email_address="bar"),
                    ],
                ),
                User(name="jack", addresses=[Address(email_address="bat")]),
            ],
        )
        s.commit()
        eq_(
            s.query(User).order_by(User.id).all(),
            [
                User(
                    name="edward",
                    addresses=[
                        Address(email_address="foo"),
                        Address(email_address="bar"),
                    ],
                ),
                User(name="jack", addresses=[Address(email_address="bat")]),
            ],
        )

    @testing.requires.savepoints
    def test_expunge_pending_on_rollback(self):
        User = self.classes.User
        sess = self.session()

        sess.begin_nested()
        u2 = User(name="newuser")
        sess.add(u2)
        assert u2 in sess
        sess.rollback()
        assert u2 not in sess

    @testing.requires.savepoints
    def test_update_deleted_on_rollback(self):
        User = self.classes.User
        s = self.session()
        u1 = User(name="ed")
        s.add(u1)
        s.commit()

        s.begin_nested()
        s.delete(u1)
        assert u1 in s.deleted
        s.rollback()
        assert u1 in s
        assert u1 not in s.deleted

    @testing.requires.savepoints_w_release
    def test_savepoint_lost_still_runs(self):
        User = self.classes.User
        s = self.session(bind=self.bind)
        trans = s.begin_nested()
        s.connection()
        u1 = User(name="ed")
        s.add(u1)

        # kill off the transaction
        nested_trans = trans._connections[self.bind][1]
        nested_trans._do_commit()

        is_(s.transaction, trans)

        with expect_warnings("nested transaction already deassociated"):
            # this previously would raise
            # "savepoint "sa_savepoint_1" does not exist", however as of
            # #5327 the savepoint already knows it's inactive
            s.rollback()

        assert u1 not in s.new

        is_(trans._state, _session.CLOSED)
        is_not_(s.transaction, trans)
        is_(s.transaction._state, _session.ACTIVE)

        is_(s.transaction.nested, False)

        is_(s.transaction._parent, None)


class AccountingFlagsTest(_LocalFixture):
    __backend__ = True

    def test_no_expire_on_commit(self):
        User, users = self.classes.User, self.tables.users

        sess = sessionmaker(expire_on_commit=False)()
        u1 = User(name="ed")
        sess.add(u1)
        sess.commit()

        testing.db.execute(
            users.update(users.c.name == "ed").values(name="edward")
        )

        assert u1.name == "ed"
        sess.expire_all()
        assert u1.name == "edward"


class AutoCommitTest(_LocalFixture):
    __backend__ = True

    def test_begin_nested_requires_trans(self):
        sess = create_session(autocommit=True)
        assert_raises(sa_exc.InvalidRequestError, sess.begin_nested)

    def test_begin_preflush(self):
        User = self.classes.User
        sess = create_session(autocommit=True)

        u1 = User(name="ed")
        sess.add(u1)

        sess.begin()
        u2 = User(name="some other user")
        sess.add(u2)
        sess.rollback()
        assert u2 not in sess
        assert u1 in sess
        assert sess.query(User).filter_by(name="ed").one() is u1

    def test_accounting_commit_fails_add(self):
        User = self.classes.User
        sess = create_session(autocommit=True)

        fail = False

        def fail_fn(*arg, **kw):
            if fail:
                raise Exception("commit fails")

        event.listen(sess, "after_flush_postexec", fail_fn)
        u1 = User(name="ed")
        sess.add(u1)

        fail = True
        assert_raises(Exception, sess.flush)
        fail = False

        assert u1 not in sess
        u1new = User(id=2, name="fred")
        sess.add(u1new)
        sess.add(u1)
        sess.flush()
        assert u1 in sess
        eq_(
            sess.query(User.name).order_by(User.name).all(),
            [("ed",), ("fred",)],
        )

    def test_accounting_commit_fails_delete(self):
        User = self.classes.User
        sess = create_session(autocommit=True)

        fail = False

        def fail_fn(*arg, **kw):
            if fail:
                raise Exception("commit fails")

        event.listen(sess, "after_flush_postexec", fail_fn)
        u1 = User(name="ed")
        sess.add(u1)
        sess.flush()

        sess.delete(u1)
        fail = True
        assert_raises(Exception, sess.flush)
        fail = False

        assert u1 in sess
        assert u1 not in sess.deleted
        sess.delete(u1)
        sess.flush()
        assert u1 not in sess
        eq_(sess.query(User.name).order_by(User.name).all(), [])

    @testing.requires.updateable_autoincrement_pks
    def test_accounting_no_select_needed(self):
        """test that flush accounting works on non-expired instances
        when autocommit=True/expire_on_commit=True."""

        User = self.classes.User
        sess = create_session(autocommit=True, expire_on_commit=True)

        u1 = User(id=1, name="ed")
        sess.add(u1)
        sess.flush()

        u1.id = 3
        u1.name = "fred"
        self.assert_sql_count(testing.db, sess.flush, 1)
        assert "id" not in u1.__dict__
        eq_(u1.id, 3)


class ContextManagerPlusFutureTest(FixtureTest):
    run_inserts = None
    __backend__ = True

    @testing.requires.savepoints
    @engines.close_open_connections
    def test_contextmanager_nested_rollback(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        sess = Session()

        def go():
            with sess.begin_nested():
                sess.add(User())  # name can't be null
                sess.flush()

        # and not InvalidRequestError
        assert_raises(sa_exc.DBAPIError, go)

        with sess.begin_nested():
            sess.add(User(name="u1"))

        eq_(sess.query(User).count(), 1)

    def test_contextmanager_commit(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        sess = Session()
        with sess.begin():
            sess.add(User(name="u1"))

        sess.rollback()
        eq_(sess.query(User).count(), 1)

    def test_contextmanager_rollback(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        sess = Session()

        def go():
            with sess.begin():
                sess.add(User())  # name can't be null

        assert_raises(sa_exc.DBAPIError, go)

        eq_(sess.query(User).count(), 0)
        sess.close()

        with sess.begin():
            sess.add(User(name="u1"))
        eq_(sess.query(User).count(), 1)

    def test_explicit_begin(self):
        s1 = Session(testing.db)
        with s1.begin() as trans:
            is_(trans, s1.transaction)
            s1.connection()

        is_(s1._transaction, None)

    def test_no_double_begin_explicit(self):
        s1 = Session(testing.db)
        s1.begin()
        assert_raises_message(
            sa_exc.InvalidRequestError,
            "A transaction is already begun on this Session.",
            s1.begin,
        )

    @testing.requires.savepoints
    def test_future_rollback_is_global(self):
        users = self.tables.users

        s1 = Session(testing.db, future=True)

        s1.begin()

        s1.connection().execute(users.insert(), [{"id": 1, "name": "n1"}])

        s1.begin_nested()

        s1.connection().execute(
            users.insert(), [{"id": 2, "name": "n2"}, {"id": 3, "name": "n3"}]
        )

        eq_(s1.connection().scalar(select(func.count()).select_from(users)), 3)

        # rolls back the whole transaction
        s1.rollback()
        is_(s1.transaction, None)

        eq_(s1.connection().scalar(select(func.count()).select_from(users)), 0)

        s1.commit()
        is_(s1.transaction, None)

    @testing.requires.savepoints
    def test_old_rollback_is_local(self):
        users = self.tables.users

        s1 = Session(testing.db)

        t1 = s1.begin()

        s1.connection().execute(users.insert(), [{"id": 1, "name": "n1"}])

        s1.begin_nested()

        s1.connection().execute(
            users.insert(), [{"id": 2, "name": "n2"}, {"id": 3, "name": "n3"}]
        )

        eq_(s1.connection().scalar(select(func.count()).select_from(users)), 3)

        # rolls back only the savepoint
        s1.rollback()

        is_(s1.transaction, t1)

        eq_(s1.connection().scalar(select(func.count()).select_from(users)), 1)

        s1.commit()
        eq_(s1.connection().scalar(select(func.count()).select_from(users)), 1)
        is_not_(s1.transaction, None)

    def test_session_as_ctx_manager_one(self):
        users = self.tables.users

        with Session(testing.db) as sess:
            is_not_(sess.transaction, None)

            sess.connection().execute(
                users.insert().values(id=1, name="user1")
            )

            eq_(
                sess.connection().execute(users.select()).all(), [(1, "user1")]
            )

            is_not_(sess.transaction, None)

        is_not_(sess.transaction, None)

        # did not commit
        eq_(sess.connection().execute(users.select()).all(), [])

    def test_session_as_ctx_manager_future_one(self):
        users = self.tables.users

        with Session(testing.db, future=True) as sess:
            is_(sess.transaction, None)

            sess.connection().execute(
                users.insert().values(id=1, name="user1")
            )

            eq_(
                sess.connection().execute(users.select()).all(), [(1, "user1")]
            )

            is_not_(sess.transaction, None)

        is_(sess.transaction, None)

        # did not commit
        eq_(sess.connection().execute(users.select()).all(), [])

    def test_session_as_ctx_manager_two(self):
        users = self.tables.users

        try:
            with Session(testing.db) as sess:
                is_not_(sess.transaction, None)

                sess.connection().execute(
                    users.insert().values(id=1, name="user1")
                )

                raise Exception("force rollback")
        except:
            pass
        is_not_(sess.transaction, None)

    def test_session_as_ctx_manager_two_future(self):
        users = self.tables.users

        try:
            with Session(testing.db, future=True) as sess:
                is_(sess.transaction, None)

                sess.connection().execute(
                    users.insert().values(id=1, name="user1")
                )

                raise Exception("force rollback")
        except:
            pass
        is_(sess.transaction, None)

    def test_begin_context_manager(self):
        users = self.tables.users

        with Session(testing.db) as sess:
            with sess.begin():
                sess.connection().execute(
                    users.insert().values(id=1, name="user1")
                )

                eq_(
                    sess.connection().execute(users.select()).all(),
                    [(1, "user1")],
                )

        # committed
        eq_(sess.connection().execute(users.select()).all(), [(1, "user1")])

    def test_sessionmaker_begin_context_manager(self):
        users = self.tables.users

        session = sessionmaker(testing.db)

        with session.begin() as sess:
            sess.connection().execute(
                users.insert().values(id=1, name="user1")
            )

            eq_(
                sess.connection().execute(users.select()).all(),
                [(1, "user1")],
            )

        # committed
        eq_(sess.connection().execute(users.select()).all(), [(1, "user1")])

    def test_begin_context_manager_rollback_trans(self):
        users = self.tables.users

        try:
            with Session(testing.db) as sess:
                with sess.begin():
                    sess.connection().execute(
                        users.insert().values(id=1, name="user1")
                    )

                    eq_(
                        sess.connection().execute(users.select()).all(),
                        [(1, "user1")],
                    )

                    raise Exception("force rollback")
        except:
            pass

        # rolled back
        eq_(sess.connection().execute(users.select()).all(), [])

    def test_begin_context_manager_rollback_outer(self):
        users = self.tables.users

        try:
            with Session(testing.db) as sess:
                with sess.begin():
                    sess.connection().execute(
                        users.insert().values(id=1, name="user1")
                    )

                    eq_(
                        sess.connection().execute(users.select()).all(),
                        [(1, "user1")],
                    )

                raise Exception("force rollback")
        except:
            pass

        # committed
        eq_(sess.connection().execute(users.select()).all(), [(1, "user1")])

    def test_sessionmaker_begin_context_manager_rollback_trans(self):
        users = self.tables.users

        session = sessionmaker(testing.db)

        try:
            with session.begin() as sess:
                sess.connection().execute(
                    users.insert().values(id=1, name="user1")
                )

                eq_(
                    sess.connection().execute(users.select()).all(),
                    [(1, "user1")],
                )

                raise Exception("force rollback")
        except:
            pass

        # rolled back
        eq_(sess.connection().execute(users.select()).all(), [])

    def test_sessionmaker_begin_context_manager_rollback_outer(self):
        users = self.tables.users

        session = sessionmaker(testing.db)

        try:
            with session.begin() as sess:
                sess.connection().execute(
                    users.insert().values(id=1, name="user1")
                )

                eq_(
                    sess.connection().execute(users.select()).all(),
                    [(1, "user1")],
                )

            raise Exception("force rollback")
        except:
            pass

        # committed
        eq_(sess.connection().execute(users.select()).all(), [(1, "user1")])


class TransactionFlagsTest(fixtures.TestBase):
    def test_in_transaction(self):
        s1 = Session(testing.db)

        eq_(s1.in_transaction(), False)

        trans = s1.begin()

        eq_(s1.in_transaction(), True)
        is_(s1.get_transaction(), trans)

        n1 = s1.begin_nested()

        eq_(s1.in_transaction(), True)
        is_(s1.get_transaction(), trans)
        is_(s1.get_nested_transaction(), n1)

        n1.rollback()

        is_(s1.get_nested_transaction(), None)
        is_(s1.get_transaction(), trans)

        eq_(s1.in_transaction(), True)

        s1.commit()

        eq_(s1.in_transaction(), False)
        is_(s1.get_transaction(), None)

    def test_in_transaction_subtransactions(self):
        """we'd like to do away with subtransactions for future sessions
        entirely.  at the moment we are still using them internally.
        it might be difficult to keep the internals working in exactly
        the same way if remove this concept, so for now just test that
        the external API works.

        """
        s1 = Session(testing.db)

        eq_(s1.in_transaction(), False)

        trans = s1.begin()

        eq_(s1.in_transaction(), True)
        is_(s1.get_transaction(), trans)

        subtrans = s1.begin(_subtrans=True)
        is_(s1.get_transaction(), trans)
        eq_(s1.in_transaction(), True)

        is_(s1._transaction, subtrans)

        s1.rollback()

        eq_(s1.in_transaction(), True)
        is_(s1._transaction, trans)

        s1.rollback()

        eq_(s1.in_transaction(), False)
        is_(s1._transaction, None)

    def test_in_transaction_nesting(self):
        s1 = Session(testing.db)

        eq_(s1.in_transaction(), False)

        trans = s1.begin()

        eq_(s1.in_transaction(), True)
        is_(s1.get_transaction(), trans)

        sp1 = s1.begin_nested()

        eq_(s1.in_transaction(), True)
        is_(s1.get_transaction(), trans)
        is_(s1.get_nested_transaction(), sp1)

        sp2 = s1.begin_nested()

        eq_(s1.in_transaction(), True)
        eq_(s1.in_nested_transaction(), True)
        is_(s1.get_transaction(), trans)
        is_(s1.get_nested_transaction(), sp2)

        sp2.rollback()

        eq_(s1.in_transaction(), True)
        eq_(s1.in_nested_transaction(), True)
        is_(s1.get_transaction(), trans)
        is_(s1.get_nested_transaction(), sp1)

        sp1.rollback()

        is_(s1.get_nested_transaction(), None)
        eq_(s1.in_transaction(), True)
        eq_(s1.in_nested_transaction(), False)
        is_(s1.get_transaction(), trans)

        s1.rollback()

        eq_(s1.in_transaction(), False)
        is_(s1.get_transaction(), None)


class NaturalPKRollbackTest(fixtures.MappedTest):
    __backend__ = True

    @classmethod
    def define_tables(cls, metadata):
        Table("users", metadata, Column("name", String(50), primary_key=True))

    @classmethod
    def setup_classes(cls):
        class User(cls.Comparable):
            pass

    def test_rollback_recover(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        session = sessionmaker()()

        u1, u2, u3 = User(name="u1"), User(name="u2"), User(name="u3")

        session.add_all([u1, u2, u3])

        session.commit()

        session.delete(u2)
        u4 = User(name="u2")
        session.add(u4)
        session.flush()

        u5 = User(name="u3")
        session.add(u5)
        with expect_warnings("New instance"):
            assert_raises(sa_exc.IntegrityError, session.flush)

        assert u5 not in session
        assert u2 not in session.deleted

        session.rollback()

    def test_reloaded_deleted_checked_for_expiry(self):
        """test issue #3677"""
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        u1 = User(name="u1")

        s = Session()
        s.add(u1)
        s.flush()
        del u1
        gc_collect()

        u1 = s.query(User).first()  # noqa

        s.rollback()

        u2 = User(name="u1")
        s.add(u2)
        s.commit()

        assert inspect(u2).persistent

    def test_key_replaced_by_update(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        u1 = User(name="u1")
        u2 = User(name="u2")

        s = Session()
        s.add_all([u1, u2])
        s.commit()

        s.delete(u1)
        s.flush()

        u2.name = "u1"
        s.flush()

        assert u1 not in s
        s.rollback()

        assert u1 in s
        assert u2 in s

        assert s.identity_map[identity_key(User, ("u1",))] is u1
        assert s.identity_map[identity_key(User, ("u2",))] is u2

    @testing.requires.savepoints
    def test_key_replaced_by_update_nested(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        u1 = User(name="u1")

        s = Session()
        s.add(u1)
        s.commit()

        with s.begin_nested():
            u2 = User(name="u2")
            s.add(u2)
            s.flush()

            u2.name = "u3"

        s.rollback()

        assert u1 in s
        assert u2 not in s

        u1.name = "u5"

        s.commit()

    def test_multiple_key_replaced_by_update(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        u1 = User(name="u1")
        u2 = User(name="u2")
        u3 = User(name="u3")

        s = Session()
        s.add_all([u1, u2, u3])
        s.commit()

        s.delete(u1)
        s.delete(u2)
        s.flush()

        u3.name = "u1"
        s.flush()

        u3.name = "u2"
        s.flush()

        s.rollback()

        assert u1 in s
        assert u2 in s
        assert u3 in s

        assert s.identity_map[identity_key(User, ("u1",))] is u1
        assert s.identity_map[identity_key(User, ("u2",))] is u2
        assert s.identity_map[identity_key(User, ("u3",))] is u3

    def test_key_replaced_by_oob_insert(self):
        users, User = self.tables.users, self.classes.User

        mapper(User, users)

        u1 = User(name="u1")

        s = Session()
        s.add(u1)
        s.commit()

        s.delete(u1)
        s.flush()

        s.execute(users.insert().values(name="u1"))
        u2 = s.query(User).get("u1")

        assert u1 not in s
        s.rollback()

        assert u1 in s
        assert u2 not in s

        assert s.identity_map[identity_key(User, ("u1",))] is u1


class JoinIntoAnExternalTransactionFixture(object):
    """Test the "join into an external transaction" examples"""

    def setup(self):
        self.connection = testing.db.connect()

        self.metadata = MetaData()
        self.table = Table(
            "t1", self.metadata, Column("id", Integer, primary_key=True)
        )
        with self.connection.begin():
            self.table.create(self.connection, checkfirst=True)

        self.setup_session()

    def test_something(self):
        A = self.A

        a1 = A()
        self.session.add(a1)
        self.session.commit()

        self._assert_count(1)

    @testing.requires.savepoints
    def test_something_with_rollback(self):
        A = self.A

        a1 = A()
        self.session.add(a1)
        self.session.flush()

        self._assert_count(1)
        self.session.rollback()
        self._assert_count(0)

        a1 = A()
        self.session.add(a1)
        self.session.commit()
        self._assert_count(1)

        a2 = A()

        self.session.add(a2)
        self.session.flush()
        self._assert_count(2)

        self.session.rollback()
        self._assert_count(1)

    def _assert_count(self, count):
        result = self.connection.scalar(
            select(func.count()).select_from(self.table)
        )
        eq_(result, count)

    def teardown(self):
        self.teardown_session()

        with self.connection.begin():
            self._assert_count(0)

        with self.connection.begin():
            self.table.drop(self.connection)

        # return connection to the Engine
        self.connection.close()


class NewStyleJoinIntoAnExternalTransactionTest(
    JoinIntoAnExternalTransactionFixture
):
    """A new recipe for "join into an external transaction" that works
    for both legacy and future engines/sessions

    """

    def setup_session(self):
        # begin a non-ORM transaction
        self.trans = self.connection.begin()

        class A(object):
            pass

        mapper(A, self.table)
        self.A = A

        # bind an individual Session to the connection
        self.session = Session(bind=self.connection, future=True)

        if testing.requires.savepoints.enabled:
            self.nested = self.connection.begin_nested()

            @event.listens_for(self.session, "after_transaction_end")
            def end_savepoint(session, transaction):
                if not self.nested.is_active:
                    self.nested = self.connection.begin_nested()

    def teardown_session(self):
        self.session.close()

        # rollback - everything that happened with the
        # Session above (including calls to commit())
        # is rolled back.
        self.trans.rollback()


class FutureJoinIntoAnExternalTransactionTest(
    NewStyleJoinIntoAnExternalTransactionTest,
    fixtures.FutureEngineMixin,
    fixtures.TestBase,
):
    pass


class NonFutureJoinIntoAnExternalTransactionTest(
    NewStyleJoinIntoAnExternalTransactionTest, fixtures.TestBase,
):
    pass


class LegacyJoinIntoAnExternalTransactionTest(
    JoinIntoAnExternalTransactionFixture, fixtures.TestBase,
):
    def setup_session(self):
        # begin a non-ORM transaction
        self.trans = self.connection.begin()

        class A(object):
            pass

        mapper(A, self.table)
        self.A = A

        # bind an individual Session to the connection
        self.session = Session(bind=self.connection)

        if testing.requires.savepoints.enabled:
            # start the session in a SAVEPOINT...
            self.session.begin_nested()

            # then each time that SAVEPOINT ends, reopen it
            @event.listens_for(self.session, "after_transaction_end")
            def restart_savepoint(session, transaction):
                if transaction.nested and not transaction._parent.nested:

                    # ensure that state is expired the way
                    # session.commit() at the top level normally does
                    # (optional step)
                    session.expire_all()

                    session.begin_nested()

    def teardown_session(self):
        self.session.close()

        # rollback - everything that happened with the
        # Session above (including calls to commit())
        # is rolled back.
        self.trans.rollback()


class LegacyBranchedJoinIntoAnExternalTransactionTest(
    LegacyJoinIntoAnExternalTransactionTest
):
    def setup_session(self):
        # begin a non-ORM transaction
        self.trans = self.connection.begin()

        class A(object):
            pass

        mapper(A, self.table)
        self.A = A

        # neutron is doing this inside of a migration
        # 1df244e556f5_add_unique_ha_router_agent_port_bindings.py
        self.session = Session(bind=self.connection.connect())

        if testing.requires.savepoints.enabled:
            # start the session in a SAVEPOINT...
            self.session.begin_nested()

            # then each time that SAVEPOINT ends, reopen it
            @event.listens_for(self.session, "after_transaction_end")
            def restart_savepoint(session, transaction):
                if transaction.nested and not transaction._parent.nested:

                    # ensure that state is expired the way
                    # session.commit() at the top level normally does
                    # (optional step)
                    session.expire_all()

                    session.begin_nested()

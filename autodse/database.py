"""
The module of result database.
"""
import os
import pickle
import queue
from threading import Lock
from time import time
from typing import Any, List, Optional, Tuple, Union

from .logger import get_default_logger
from .result import ResultBase

LOG = get_default_logger('Database')


class Database():
    """The base class of result database with API definitions"""

    def __init__(self, name: str, cache_size: int = 1, db_file_path: Optional[str] = None):
        self.db_id = '{0}-{1}'.format(name, int(time()))

        # Set the database name to default if not specified
        if db_file_path is None:
            self.db_file_path = '{0}/{1}.db'.format(os.getcwd(), name)
            LOG.warning('No file name was given for dumping the database, dumping to %s',
                        self.db_file_path)
        else:
            self.db_file_path = db_file_path

        # Current best result set (min heap)
        self.best_cache: queue.PriorityQueue = queue.PriorityQueue(cache_size)

    def update_best(self, result: ResultBase) -> None:
        """Check if the new result has the best QoR and update it if so.

        Parameters
        ----------
        result:
            The new result to be checked.
        """

        if result.valid:
            if self.best_cache.qsize() < self.best_cache.maxsize:
                self.best_cache.put((result.quality, result))
            else:
                last_quality, last_result = self.best_cache.get()
                if result.quality > last_quality:
                    self.best_cache.put((result.quality, result))
                else:
                    self.best_cache.put((last_quality, last_result))

    def commit_best(self) -> None:
        """Commit the best cache for persisting"""

        self.commit_impl('meta-best-cache', self.best_cache.queue)

    def commit(self, key: str, result: Any) -> None:
        """Commit a new result to the database

        Parameters
        ----------
        key:
            The key of a design point.

        result:
            The result to be committed.
        """

        if not self.commit_impl(key, result):
            LOG.error('Failed to commit results to the database')
            raise RuntimeError()
        self.update_best(result)

    def batch_commit(self, pairs: List[Tuple[str, Any]]) -> None:
        """Commit a set of new results to the database.

        Parameters
        ----------
        pairs:
            A list of key-result pairs
        """

        if self.batch_commit_impl(pairs) != len(pairs):
            LOG.error('Failed to commit results to the database')
            raise RuntimeError()

        # Update the best result
        for _, result in pairs:
            self.update_best(result)

    def load(self) -> None:
        """Load existing data from the given database and update the best cahce (if available)"""
        raise NotImplementedError()

    def query(self, key: str) -> Optional[Any]:
        """Query for the value by the given key

        Parameters
        ----------
        key:
            The key of a design point.

        Returns
        -------
        Optional[Any]:
            The result object of the corresponding key, or None if the key is unavailable.
        """
        raise NotImplementedError()

    def batch_query(self, keys: List[str]) -> List[Optional[Any]]:
        """Query for a list of the values by the given key list

        Parameters
        ----------
        key:
            The key of a design point.

        Returns
        -------
        Optional[Any]:
            The result object of the corresponding key, or None if the key is unavailable.
        """
        raise NotImplementedError()

    def commit_impl(self, key: str, result: Any) -> bool:
        """Commit function implementation.

        Parameters
        ----------
        key:
            The key of a design point.

        result:
            The result to be committed.

        Returns
        -------
        bool:
            True if the commit was success; otherwise False.
        """
        raise NotImplementedError()

    def batch_commit_impl(self, pairs: List[Tuple[str, Any]]) -> int:
        """Batch commit function implementation.

        Parameters
        ----------
        pairs:
            A list of key-result pairs.

        Returns
        -------
        int:
            Indicate the number of committed data.
        """
        raise NotImplementedError()

    def count(self) -> int:
        """Count total number of data points in the database

        Returns
        -------
        int:
            Total number of data points
        """
        raise NotImplementedError()

    def persist(self) -> bool:
        """Persist the DB by dumping it to a pickle file and close the DB

        Returns
        -------
        bool:
            True if the dumping and close was success; otherwise False.

        """
        raise NotImplementedError()


class RedisDatabase(Database):
    """The database implementation using Redis"""

    def __init__(self, name: str, cache_size: int = 1, db_file_path: Optional[str] = None):
        super(RedisDatabase, self).__init__(name, cache_size, db_file_path)

        import redis

        #TODO: scale-out
        self.database = redis.StrictRedis(host='localhost', port=6379)

        # Check the connection
        try:
            self.database.client_list()
        except redis.ConnectionError:
            LOG.error('Failed to connect to Redis database')
            self.database = None
            raise RuntimeError()

    def load(self) -> None:
        #pylint:disable=missing-docstring

        # Load existing data
        # Note that the dumped data for RedisDatabase should be in pickle format
        if os.path.exists(self.db_file_path):
            with open(self.db_file_path, 'rb') as filep:
                try:
                    data = pickle.load(filep)
                except ValueError as err:
                    LOG.error('Failed to initialize the database: %s', str(err))
                    raise RuntimeError()
            LOG.info('Load %d data from an existing database', len(data))
            self.database.hmset(self.db_id, data)

        # Update the best cache
        if self.count() > 0:
            best_cache = self.query('meta-best-cache')
            if not best_cache:
                LOG.warning('Key "meta-best-cache" is missing in the DB. '
                            'The best result may not be real.')
            else:
                for quality, result in best_cache:
                    try:
                        self.best_cache.put((quality, result), timeout=0.1)
                    except queue.Full:
                        return

    def __del__(self):
        """Delete the data we generated in Redis database"""
        if self.database:
            self.database.delete(self.db_id)
            if self.database.exists(self.db_id):
                LOG.warning('Fail to cleanup the Redis database')
            else:
                LOG.info('Detach and cleanup the Redis database')

    def query(self, key: str) -> Optional[Any]:
        #pylint:disable=missing-docstring

        if not self.database.hexists(self.db_id, key):
            return None

        pickled_obj = self.database.hget(self.db_id, key)
        if pickled_obj:
            try:
                return pickle.loads(pickled_obj)
            except ValueError as err:
                LOG.error('Failed to deserialize the result of %s: %s', key, str(err))
        return None

    def batch_query(self, keys: List[str]) -> List[Optional[Any]]:
        #pylint:disable=missing=docstring

        data = []
        for key, pickled_obj in zip(keys, self.database.hmget(self.db_id, keys)):
            if pickled_obj:
                try:
                    data.append(pickle.loads(pickled_obj))
                except ValueError as err:
                    LOG.error('Failed to deserialize the result of %s: %s', key, str(err))
                    data.append(None)
            else:
                data.append(None)
        return data

    def commit_impl(self, key: str, result: Any) -> bool:
        #pylint:disable=missing-docstring

        pickled_result = pickle.dumps(result)
        self.database.hset(self.db_id, key, pickled_result)
        return True

    def batch_commit_impl(self, pairs: List[Tuple[str, Any]]) -> int:
        #pylint:disable=missing-docstring

        data = {key: pickle.dumps(result) for key, result in pairs}
        self.database.hmset(self.db_id, data)
        return len(data)

    def count(self) -> int:
        #pylint:disable=missing-docstring
        return len(self.database.hkeys(self.db_id))

    def persist(self) -> bool:
        #pylint:disable=missing-docstring

        dump_db = {
            key: self.database.hget(self.db_id, key)
            for key in self.database.hgetall(self.db_id)
        }
        with open(self.db_file_path, 'wb') as filep:
            pickle.dump(dump_db, filep, pickle.HIGHEST_PROTOCOL)

        return True


class PickleDatabase(Database):
    """
    The database implementation using PickleDB

    This is an alternative when other databases are unavailable in the system.
    Note that it is discouraged to use this database for DSE due to poor performance
    and the lack of multi-node support.
    """

    def __init__(self, name: str, cache_size: int = 1, db_file_path: Optional[str] = None):
        super(PickleDatabase, self).__init__(name, cache_size, db_file_path)

        import pickledb
        self.lock = Lock()
        self.database: pickledb.PickleDB = {}

        try:
            # Load the Pickle database
            # Note that we cannot enable auto dump since we will pickle all data before persisting
            self.database = pickledb.load(self.db_file_path, False)
        except ValueError as err:
            LOG.error('Failed to initialize the database: %s', str(err))
            raise RuntimeError()

    def load(self) -> None:
        #pylint:disable=missing-docstring
        import jsonpickle

        try:
            # Decode objects
            for key in self.database.getall():
                obj = jsonpickle.decode(self.database.get(key))
                self.database.set(key, obj)
        except ValueError as err:
            LOG.error('Failed to load the data from the database: %s', str(err))
            raise RuntimeError()

        # Update the best cache
        if self.count() > 0:
            best_cache = self.query('meta-best-cache')
            if not best_cache:
                LOG.warning('Key "meta-best-cache" is missing in the DB. '
                            'The best result may not be real.')
            else:
                for quality, result in best_cache:
                    try:
                        self.best_cache.put((quality, result), timeout=0.1)
                    except queue.Full:
                        return

    def query(self, key: str) -> Optional[Any]:
        #pylint:disable=missing-docstring

        self.lock.acquire()
        value: Union[bool, ResultBase] = self.database.get(key)
        self.lock.release()
        return None if isinstance(value, bool) else value

    def batch_query(self, keys: List[str]) -> List[Optional[Any]]:
        #pylint:disable=missing=docstring

        self.lock.acquire()
        values: List[Optional[Any]] = []
        for key in keys:
            value = self.database.get(key)
            values.append(None if isinstance(value, bool) else value)
        self.lock.release()
        return values

    def commit_impl(self, key: str, result: ResultBase) -> bool:
        #pylint:disable=missing-docstring

        self.lock.acquire()
        self.database.exists(key)
        self.database.set(key, result)
        self.lock.release()
        return True

    def batch_commit_impl(self, pairs: List[Tuple[str, Any]]) -> int:
        #pylint:disable=missing-docstring

        self.lock.acquire()
        for key, result in pairs:
            self.database.set(key, result)
        self.lock.release()
        return len(pairs)

    def count(self) -> int:
        #pylint:disable=missing-docstring
        return self.database.totalkeys()

    def persist(self) -> bool:
        #pylint:disable=missing-docstring

        import jsonpickle
        # Pickle all results so that they are JSON seralizable
        for key in self.database.getall():
            pickled_obj = jsonpickle.encode(self.database.get(key))
            self.database.set(key, pickled_obj)

        return self.database.dump()

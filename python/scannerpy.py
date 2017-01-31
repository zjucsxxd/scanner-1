import toml
import os
import os.path
import sys
import grpc
import struct
import numpy as np
import logging as log
from enum import Enum
from random import choice
from string import ascii_uppercase
from collections import defaultdict

class DeviceType(Enum):
    CPU = 0
    GPU = 1

    @staticmethod
    def to_proto(db, device):
        if device == DeviceType.CPU:
            return db._metadata_types.CPU
        elif device == DeviceType.GPU:
            return db._metadata_types.GPU
        else:
            log.critical('Invalid device type')
            exit()


class Config(object):
    """ TODO(wcrichto): document me """

    def __init__(self, config_path=None):
        log.basicConfig(
            level=log.DEBUG,
            format='%(levelname)7s %(asctime)s %(filename)s:%(lineno)03d] %(message)s')
        self.config_path = config_path or self.default_config_path()
        config = self.load_config(self.config_path)
        try:
            self.scanner_path = config['scanner_path']
            sys.path.append('{}/build'.format(self.scanner_path))
            sys.path.append('{}/thirdparty/build/bin/storehouse/lib'
                            .format(self.scanner_path))

            from storehousepy import StorageConfig, StorageBackend
            storage = config['storage']
            storage_type = storage['type']
            self.db_path = str(storage['db_path'])
            if storage_type == 'posix':
                storage_config = StorageConfig.make_posix_config()
            elif storage_type == 'gcs':
                with open(storage['key_path']) as f:
                    key = f.read()
                storage_config = StorageConfig.make_gcs_config(
                    storage['cert_path'].encode('latin-1'),
                    key,
                    storage['bucket'].encode('latin-1'))
            else:
                log.critical('Unsupported storage type {}'.format(storage_type))
                exit()

            from scanner.metadata_pb2 import MemoryPoolConfig
            self.memory_pool_config = MemoryPoolConfig()
            if 'memory_pool' in config:
                memory_pool = config['memory_pool']
                self.memory_pool_config.use_pool = memory_pool['use_pool']
                if self.memory_pool_config.use_pool:
                    self.memory_pool_config.pool_size = memory_pool['pool_size']
            else:
                self.memory_pool_config.use_pool = False

            if 'network' in config:
                network = config['network']
                if 'master_address' in network:
                    self.master_address = network['master_address']
                else:
                    self.master_address = 'localhost:5001'
            else:
                self.master_address = 'localhost:5001'

        except KeyError as key:
            log.critical('Scanner config missing key: {}'.format(key))
            exit()
        self.storage_config = storage_config
        self.storage = StorageBackend.make_from_config(storage_config)

    @staticmethod
    def default_config_path():
        return '{}/.scanner.toml'.format(os.path.expanduser('~'))

    def load_config(self, path):
        try:
            with open(path, 'r') as f:
                return toml.loads(f.read())
        except IOError:
            log.critical('Error: you need to setup your Scanner config. Run `python scripts/setup.py`.')
            exit()


class Database:
    def __init__(self, config_path=None):
        self.config = Config(config_path)

        # Load all protobuf types
        import scanner.metadata_pb2 as metadata_types
        import scanner.engine.rpc_pb2 as rpc_types
        import scanner.kernels.args_pb2 as arg_types
        import scanner_bindings as bindings
        self._metadata_types = metadata_types
        self._rpc_types = rpc_types
        self._arg_types = arg_types
        self._bindings = bindings

        # Setup database metadata
        self._db_path = self.config.db_path
        self._storage = self.config.storage
        self._master_address = self.config.master_address
        self._db_params = self._bindings.make_database_parameters(
            self.config.storage_config,
            self.config.memory_pool_config.SerializeToString(),
            self._db_path)
        self.evaluators = EvaluatorGenerator(self)

        # Initialize database if it does not exist
        pydb_path = '{}/pydb'.format(self._db_path)
        if not os.path.isfile('{}/db_metadata.bin'.format(self._db_path)):
            self._bindings.create_database(self.config.storage_config, self._db_path)
            os.mkdir(pydb_path)
            self._collections = self._metadata_types.CollectionsDescriptor()
            self._update_collections()

        if not os.path.isdir(pydb_path):
            log.critical('Scanner database at {} was not made via Python'.format(self._db_path))
            exit()

        # Load database descriptors from disk
        self._collections = self._load_descriptor(
            self._metadata_types.CollectionsDescriptor,
            'pydb/descriptor.bin')

        # Initialize gRPC channel with master server
        channel = grpc.insecure_channel(self._master_address)
        self._master = self._rpc_types.MasterStub(channel)

        # Ping master and start master/worker locally if they don't exist.
        try:
            self._master.Ping(self._rpc_types.Empty())
        except grpc.RpcError as e:
            status = e.code()
            if status == grpc.StatusCode.UNAVAILABLE:
                log.warn("Master not started, creating temporary master/worker")
                # If they get GC'd then the masters/workers will die, so persist
                # them until the database object dies
                self._ignore1 = self.start_master()
                self._ignore2 = self.start_worker()
            elif status == grpc.StatusCode.OK:
                pass
            else:
                log.critical('Master ping errored with status: {}'.format(status))
                exit()

    def get_include(self):
        dirs = self._bindings.get_include().split(";")
        return " ".join(["-I " + d for d in dirs])

    def _load_descriptor(self, descriptor, path):
        d = descriptor()
        d.ParseFromString(self._storage.read('{}/{}'.format(self._db_path, path)))
        return d

    def _save_descriptor(self, descriptor, path):
        self._storage.write(
            '{}/{}'.format(self._db_path, path),
            descriptor.SerializeToString())

    def _load_db_metadata(self):
        return self._load_descriptor(
            self._metadata_types.DatabaseDescriptor,
            'db_metadata.bin')

    def start_master(self):
        return self._bindings.start_master(self._db_params)

    def start_worker(self, master_address=None):
        return self._bindings.start_worker(self._db_params, self._master_address)

    def _update_collections(self):
        self._save_descriptor(self._collections, 'pydb/descriptor.bin')

    def new_collection(self, collection_name, table_names):
        if collection_name in self._collections.names:
            log.critical('Collection with name {} already exists' \
                             .format(collection_name))
            exit()

        last_id = self._collections.ids[-1] if len(self._collections.ids) > 0 else -1
        new_id = last_id + 1
        self._collections.ids.append(new_id)
        self._collections.names.append(collection_name)
        self._update_collections()
        collection = self._metadata_types.CollectionDescriptor()
        collection.tables.extend(table_names)
        self._save_descriptor(collection, 'pydb/collection_{}.bin'.format(new_id))

        return self.get_collection(collection_name)

    def get_collection(self, name):
        index = self._collections.names[:].index(name)
        id = self._collections.ids[index]
        collection = self._load_descriptor(
            self._metadata_types.CollectionDescriptor,
            'pydb/collection_{}.bin'.format(index))
        return Collection(self, name, collection)

    def ingest_video(self, table_name, video):
        self._bindings.ingest_videos(
            self.config.storage_config,
            self._db_path,
            [table_name],
            [video])

    def ingest_video_collection(self, collection_name, videos):
        table_names = ['{}_{:03d}'.format(collection_name, i)
                       for i in range(len(videos))]
        self._bindings.ingest_videos(
            self.config.storage_config,
            self._db_path,
            table_names,
            videos)
        return self.new_collection(collection_name, table_names)

    def sampler(self):
        return Sampler(self)

    def table(self, table_name):
        db_meta = self._load_db_metadata()

        if isinstance(table_name, basestring):
            table_id = None
            for table in db_meta.tables:
                if table.name == table_name:
                    table_id = table.id
                    break
            if table_id is None:
                log.critical('Table with name {} not found'.format(table_name))
                exit()
        elif isinstance(table_name, int):
            table_id = table_name
        else:
            log.critical('Invalid table identifier')
            exit()

        descriptor = self._load_descriptor(
            self._metadata_types.TableDescriptor,
            'tables/{}/descriptor.bin'.format(table_id))
        return Table(self, descriptor)

    def _toposort(self, evaluator):
        edges = defaultdict(list)
        in_edges_left = defaultdict(int)
        start_node = None

        explored_nodes = set()
        stack = [evaluator]
        while len(stack) > 0:
            c = stack.pop()
            explored_nodes.add(c)
            if (c._name == "InputTable"):
                start_node = c
                continue
            elif len(c._inputs) == 0:
                input = Evaluator.input(self)
                # TODO(wcrichto): determine input columns from dataset
                c._inputs = [(input, ["frame", "frame_info"])]
                start_node = input
            for (parent, _) in c._inputs:
                edges[parent].append(c)
                in_edges_left[c] += 1

                if parent not in explored_nodes:
                    stack.append(parent)

        eval_sorted = []
        eval_index = {}
        stack= [start_node]
        while len(stack) > 0:
            c = stack.pop()
            eval_sorted.append(c)
            eval_index[c] = len(eval_sorted) - 1
            for child in edges[c]:
                in_edges_left[child] -= 1
                if in_edges_left[child] == 0:
                    stack.append(child)

        return [e.to_proto(eval_index) for e in eval_sorted]

    def _process_dag(self, evaluator):
        # If evaluators are passed as a list (e.g. [transform, caffe])
        # then hook up inputs to outputs of adjacent evaluators
        if isinstance(evaluator, list):
            for i in range(len(evaluator) - 1):
                out_cols = self._bindings.get_output_columns(evaluator[i]._name)
                evaluator[i+1]._inputs = [(evaluator[i], out_cols)]
            evaluator = evaluator[-1]

        # If the user doesn't explicitly specify an OutputTable, assume that
        # it's all the output columns of the last evaluator.
        if evaluator._name != "OutputTable":
            out_cols = self._bindings.get_output_columns(str(evaluator._name))
            evaluator = Evaluator.output(self, [(evaluator, out_cols)])

        return self._toposort(evaluator)

    def run(self, tasks, evaluator, output_collection=None, job_name=None):
        # If the input is a collection, assume user is running over all frames
        input_is_collection = isinstance(tasks, Collection)
        if input_is_collection:
            sampler = self.sampler()
            tables = [(t, t.replace(tasks.name(), output_collection))
                      for t in tasks.table_names()]
            tasks = sampler.all_frames(tables)

        job_params = self._rpc_types.JobParameters()
        # Generate a random job name if none given
        job_name = job_name or ''.join(choice(ascii_uppercase) for _ in range(12))
        job_params.job_name = job_name
        job_params.task_set.tasks.extend(tasks)
        job_params.task_set.evaluators.extend(self._process_dag(evaluator))

        # Execute job via RPC
        try:
            self._master.NewJob(job_params)
        except grpc.RpcError as e:
            log.critical('Job failed with error: {}'.format(e))
            exit()

        # Return a new collection if the input was a collection, otherwise
        # return a table list
        table_names = [task.output_table_name for task in tasks]
        if input_is_collection:
            return self.new_collection(output_collection, table_names)
        else:
            return [self.table(t) for t in table_names]


class Sampler:
    def __init__(self, db):
        self._db = db

    def all_frames(self, videos):
        tasks = []
        for (input_table, output_table) in videos:
            task = self._db._metadata_types.Task()
            task.output_table_name = output_table
            row_count = 100 # TODO(wcrichto): extract this
            column_names = ["frame", "frame_info"] # TODO(wcrichto): extract this
            sample = task.samples.add()
            sample.table_name = input_table
            sample.column_names.extend(column_names)
            sample.rows.extend(range(row_count))
            tasks.append(task)
        return tasks


class EvaluatorGenerator:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        if not self._db._bindings.has_evaluator(name):
            log.critical('Evaluator {} does not exist'.format(name))
            exit()
        def make_evaluator(**kwargs):
            inputs = kwargs.pop('inputs', [])
            device = kwargs.pop('device', None)
            if device is None:
                log.critical('Must specify device type')
                exit()
            return Evaluator(self._db, name, inputs, device, kwargs)
        return make_evaluator


class Evaluator:
    def __init__(self, db, name, inputs, device, args):
        self._db = db
        self._name = name
        self._inputs = inputs
        self._device = device
        self._args = args

    @classmethod
    def input(cls, db):
        # TODO(wcrichto): allow non-frame inputs
        return cls(db, "InputTable", [(None, ["frame", "frame_info"])],
                   DeviceType.CPU, {})

    @classmethod
    def output(cls, db, inputs):
        return cls(db, "OutputTable", inputs, DeviceType.CPU, {})

    def to_proto(self, indices):
        e = self._db._metadata_types.Evaluator()
        e.name = self._name

        for (in_eval, cols) in self._inputs:
            inp = e.inputs.add()
            idx = indices[in_eval] if in_eval is not None else -1
            inp.evaluator_index = idx
            inp.columns.extend(cols)

        e.device_type = DeviceType.to_proto(self._db, self._device)

        if len(self._args) > 0:
            proto_name = self._name + 'Args'
            if not hasattr(self._db._arg_types, proto_name):
                log.critical('Missing protobuf {}'.format(proto_name))
                exit()
            args = getattr(self._db._arg_types, proto_name)()
            for k, v in self._args.iteritems():
                setattr(args, k, v)
            e.kernel_args = args.SerializeToString()

        return e


class Collection:
    def __init__(self, db, name, descriptor):
        self._db = db
        self._name = name
        self._descriptor = descriptor

    def name(self):
        return self._name

    def table_names(self):
        return list(self._descriptor.tables)

    def tables(self, index=None):
        tables = [self._db.table(t) for t in self._descriptor.tables]
        return tables[index] if index is not None else tables


class Column:
    def __init__(self, table, descriptor):
        self._table = table
        self._descriptor = descriptor
        self._storage = table._db.config.storage
        self._db_path = table._db.config.db_path

    def name(self):
        return self._descriptor.name

    def _load_output_file(self, item_id, rows):
        assert len(rows) > 0

        contents = self._storage.read(
            '{}/tables/{}/{}_{}.bin'.format(
                self._db_path, self._table._descriptor.id,
                self._descriptor.id, item_id))

        lens = []
        start_pos = None
        pos = 0
        (num_rows,) = struct.unpack("l", contents[:8])

        i = 8
        rows = rows if len(rows) > 0 else range(num_rows)
        for fi in rows:
            (buf_len,) = struct.unpack("l", contents[i:i+8])
            i += 8
            old_pos = pos
            pos += buf_len
            if start_pos is None:
                start_pos = old_pos
            lens.append(buf_len)

        i = 8 + num_rows * 8 + start_pos
        for buf_len in lens:
            buf = contents[i:i+buf_len]
            i += buf_len
            yield buf

    def load(self):
        table_descriptor = self._table._descriptor
        total_rows = table_descriptor.num_rows
        rows_per_item = table_descriptor.rows_per_item

        # Integer divide, round up
        num_items = (total_rows + rows_per_item // 2) // rows_per_item
        bufs = []
        for item_id in range(num_items):
            rows = total_rows % rows_per_item if item_id == num_items - 1 else rows_per_item
            for output in self._load_output_file(item_id, range(rows)):
                yield output


class Table:
    def __init__(self, db, descriptor):
        self._db = db
        self._descriptor = descriptor

    def columns(self, index=None):
        columns = [Column(self, c) for c in self._descriptor.columns]
        return columns[index] if index is not None else columns

    # Convenience method for loading frames
    def load_frames(self, frame_col=None, frame_info_col=None):
        # Assume frame and frame_info are first and second columns, respectively
        # if not given explicitly
        if frame_col is None:
            frame_col = self.columns(0)
            frame_info_col = self.columns(1)
        for (frame_s, frame_info_s) in zip(frame_col.load(), frame_info_col.load()):
            frame_info = self._db._metadata_types.FrameInfo()
            frame_info.ParseFromString(frame_info_s)
            frame = np.frombuffer(frame_s, dtype=np.dtype(np.uint8))
            frame.resize((frame_info.height, frame_info.width, 3))
            yield frame
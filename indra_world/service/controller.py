import logging
import datetime
from indra_world.sources.dart import process_reader_output, DartClient
from indra_world.assembly.incremental_assembler import \
    IncrementalAssembler
from indra_world.resources import get_resource_file
from indra.pipeline import AssemblyPipeline
from indra_world.assembly.operations import *
from .db import DbManager


logger = logging.getLogger(__name__)

preparation_pipeline = AssemblyPipeline.from_json_file(
    get_resource_file('statement_preparation.json'))


expected_readers = {'eidos', 'hume', 'sofia'}


class ServiceController:
    def __init__(self, db_url, dart_client=None):
        self.db = DbManager(db_url)
        self.assemblers = {}
        self.assembly_triggers = {}
        if dart_client:
            self.dart_client = dart_client
        else:
            self.dart_client = DartClient(storage_mode='web')

    def new_project(self, project_id, name, corpus_id=None):
        res = self.db.add_project(project_id, name)
        if res is None:
            return None
        if corpus_id:
            record_keys = self.db.get_records_for_corpus(corpus_id)
            return self.db.add_records_for_project(project_id, record_keys)

    def load_project(self, project_id, record_keys=None):
        # 1. Select records associated with project
        if record_keys is None:
            record_keys = self.db.get_records_for_project(project_id)
        # 2. Select statements from prepared stmts table
        prepared_stmts = []
        for record_key in record_keys:
            prepared_stmts += self.db.get_statements_for_record(record_key)
        # 3. Select curations for project
        curations = self.get_project_curations(project_id)
        # 4. Initiate an assembler
        assembler = IncrementalAssembler(prepared_stmts, curations=curations)
        self.assemblers[project_id] = assembler

    def unload_project(self, project_id):
        self.assemblers.pop(project_id, None)

    def get_projects(self):
        return self.db.get_projects()

    def get_project_records(self, project_id):
        return self.db.get_records_for_project(project_id)

    def add_dart_record(self, record, date=None):
        if date is None:
            date = datetime.datetime.utcnow().isoformat()
        return self.db.add_dart_record(reader=record['identity'],
                                       reader_version=record['version'],
                                       document_id=record['document_id'],
                                       storage_key=record['storage_key'],
                                       date=date)

    def process_dart_record(self, record, grounding_mode='compositional',
                            extract_filter='influence'):
        reader_output = self.dart_client.get_output_from_record(record)
        return self.add_reader_output(reader_output, record,
                                      grounding_mode=grounding_mode,
                                      extract_filter=extract_filter)

    def add_reader_output(self, content, record,
                          grounding_mode='compositional',
                          extract_filter='influence'):
        stmts = process_reader_output(record['identity'], content,
                                      record['document_id'],
                                      grounding_mode=grounding_mode,
                                      extract_filter=extract_filter)
        return self.add_reader_statements(stmts, record)

    def add_reader_statements(self, stmts, record):
        prepared_stmts = preparation_pipeline.run(stmts)
        return self.add_prepared_statements(prepared_stmts,
                                            record['storage_key'])

    def add_prepared_statements(self, prepared_stmts, record_key):
        return self.db.add_statements_for_record(record_key=record_key,
                                                 # FIXME: how should we set the
                                                 # version here?
                                                 indra_version='1.0',
                                                 stmts=prepared_stmts)

    def assemble_new_records(self, project_id, new_record_keys):
        # 1. We get all the records associated with the project
        # which may or may not include some of the new ones
        logger.info('Getting records for project')
        record_keys = self.db.get_records_for_project(project_id)
        old_record_keys = list(set(record_keys) - set(new_record_keys))
        # 2. Now load the project with the old record keys
        logger.info('Loading the project with its existing statements')
        self.load_project(project_id, old_record_keys)
        # 3. Now get the new statements associated with the new records
        new_stmts = []
        for record_key in new_record_keys:
            stmts = self.db.get_statements_for_record(record_key)
            new_stmts += stmts
        # 4. Finally get an incremental assembly delta and return it
        logger.info('Running incremental assembly')
        delta = self.assemblers[project_id].add_statements(new_stmts)
        logger.info('Got assembly delta, returning')
        return delta

    def add_curations(self, project_id, curations):
        # Note: since loading a project applies all existing curations, it's
        # very important that this happens first, before the new curations
        # are added to the DB
        self.load_project(project_id)
        # We now add new curations to the DB
        for stmt_hash, curation in curations.items():
            self.db.add_curation_for_project(project_id, stmt_hash, curation)
        matches_hash_map = \
            self.assemblers[project_id].get_curation_effects(curations)
        # We need to *unload* the project here if it is currently loaded
        # since that is the cleanest way to guarantee that it will be
        # reloaded and the new curation applied correctly (in the
        # IncrementalAssembler's constructor).
        self.unload_project(project_id)
        return matches_hash_map

    def get_project_curations(self, project_id):
        return self.db.get_curations_for_project(project_id)

    def add_project_records(self, project_id, record_keys):
        return self.db.add_records_for_project(project_id, record_keys)

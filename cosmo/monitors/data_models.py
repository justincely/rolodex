import pandas as pd
import numpy as np
import os
from glob import glob

from typing import List
from monitorframe.datamodel import BaseDataModel
from peewee import OperationalError

from ..filesystem import find_files, data_from_exposures, data_from_jitters
from ..sms import SMSTable
from .. import SETTINGS

FILES_SOURCE = SETTINGS['filesystem']['source']
PROGRAMS = SETTINGS['dark_programs']


def dgestar_to_fgs(results: List[dict]) -> None:
    """Add a FGS key to each row dictionary."""
    for item in results:
        item.update({
            'FGS': item['DGESTAR'][-2:]
            })  # The dominant guide star key is the last 2  # values in the
        # string


class AcqDataModel(BaseDataModel):
    """Datamodel for Acq files."""
    files_source = FILES_SOURCE
    subdir_pattern = '?????'
    primary_key = 'ROOTNAME'

    def get_new_data(self):
        header_request = {
            0: ['ACQSLEWX', 'ACQSLEWY', 'ROOTNAME', 'PROPOSID', 'OBSTYPE',
                'SHUTTER', 'LAMPEVNT', 'ACQSTAT', 'EXTENDED', 'LINENUM',
                'APERTURE', 'OPT_ELEM', 'LIFE_ADJ', 'CENWAVE', 'DETECTOR',
                'EXPTYPE'], 1: ['EXPSTART', 'NEVENTS']
            }

        # Different ACQ types may not have the full set
        header_defaults = {
            'ACQSLEWX': 0.0, 'ACQSLEWY': 0.0, 'NEVENTS': 0.0, 'LAMPEVNT': 0.0
            }

        # SPT file header keys, extensions
        spt_header_request = {0: ['DGESTAR']}

        files = find_files('*rawacq*', data_dir=self.files_source,
                           subdir_pattern=self.subdir_pattern)

        if self.model is not None:
            currently_ingested = [item.FILENAME for item in
                                  self.model.select(self.model.FILENAME)]

            for file in currently_ingested:
                files.remove(file)

        if not files:  # No new files
            return pd.DataFrame()

        data_results = data_from_exposures(files,
                                           header_request=header_request,
                                           header_defaults=header_defaults,
                                           spt_header_request=spt_header_request, )

        dgestar_to_fgs(data_results)

        return data_results


class OSMDataModel(BaseDataModel):
    """Data model for all OSM Shift monitors."""
    files_source = FILES_SOURCE
    subdir_pattern = '?????'

    cosmo_layout = True

    primary_key = 'ROOTNAME'

    def get_new_data(self):
        """Retrieve data."""
        header_request = {
            0: ['ROOTNAME', 'DETECTOR', 'LIFE_ADJ', 'OPT_ELEM', 'CENWAVE',
                'FPPOS', 'PROPOSID', 'OBSET_ID'], 1: ['EXPSTART']
            }

        table_request = {1: ['TIME', 'SHIFT_DISP', 'SHIFT_XDISP', 'SEGMENT']}

        reference_request = {
            'LAMPTAB': {
                'match_keys': ['OPT_ELEM', 'CENWAVE', 'FPOFFSET'],
                'table_request': {1: ['SEGMENT', 'FP_PIXEL_SHIFT']},
                }, 'WCPTAB': {
                'match_keys': ['OPT_ELEM'],
                'table_request': {1: ['XC_RANGE', 'SEARCH_OFFSET']}
                }
            }

        files = find_files('*lampflash*', data_dir=self.files_source,
                           subdir_pattern=self.subdir_pattern)

        if self.model is not None:
            currently_ingested = [item.FILENAME for item in
                                  self.model.select(self.model.FILENAME)]

            for file in currently_ingested:
                files.remove(file)

        if not files:  # No new files
            return pd.DataFrame()

        data_results = pd.DataFrame(
            data_from_exposures(files, header_request=header_request,
                                table_request=table_request,
                                reference_request=reference_request))

        # Remove any rows that have empty data columns
        data_results = data_results.drop(data_results[data_results.apply(
            lambda x: not bool(len(x.SHIFT_DISP)),
            axis=1)].index.values).reset_index(drop=True)

        # Add tsince data from SMSTable.
        try:
            sms_data = pd.DataFrame(
                SMSTable.select(SMSTable.ROOTNAME, SMSTable.TSINCEOSM1,
                                SMSTable.TSINCEOSM2).where(
                    # x << y -> x IN y (y must be a list)
                    SMSTable.ROOTNAME + 'q' <<
                    data_results.ROOTNAME.to_list()).dicts())

        except OperationalError as e:
            raise type(e)(str(e) + '\nSMS database is required.')

        # It's possible that there could be a lag in between when the SMS
        # data is updated and when new lampflashes
        # are added.
        # Returning the empty data frame ensures that only files with a
        # match in the SMS data are added...
        # This may not be the best idea
        if sms_data.empty:
            return sms_data

        # Need to add the 'q' at the end of the rootname.. For some reason
        # those are missing from the SMS rootnames
        sms_data.ROOTNAME += 'q'

        # Combine the data from the files with the data from the SMS table
        # with an inner merge between the two.
        # NOTE: this means that if a file does not have a corresponding
        # entry in the SMSTable, it will not be in the
        # dataset used for monitoring.
        merged = pd.merge(data_results, sms_data, on='ROOTNAME')

        return merged


class JitterDataModel(BaseDataModel):
    files_source = FILES_SOURCE
    subdir_pattern = '?????'

    def get_new_data(self):
        primary_header_keys = ('PROPOSID', 'CONFIG')
        extension_header_keys = ('EXPNAME',)

        data_keys = ('SI_V2_AVG', 'SI_V3_AVG')
        reduce = {
            'SI_V2_AVG': ('mean', 'std', 'max'),
            'SI_V3_AVG': ('mean', 'std', 'max')
            }

        files = find_files('*jit*', data_dir=self.files_source,
                           subdir_pattern=self.subdir_pattern)

        if self.model is not None:
            currently_ingested = [item.FILENAME for item in
                                  self.model.select(self.model.FILENAME)]

            for file in currently_ingested:
                files.remove(file)

        if not files:  # No new files
            return pd.DataFrame()

        data_results = pd.DataFrame(
            data_from_jitters(files, primary_header_keys,
                              extension_header_keys, data_keys,
                              reduce_to_stats=reduce))

        # Remove any NaNs or inf that may occur from the statistics
        # calculations.
        data_results = data_results.replace([np.inf, -np.inf],
                                            np.nan).dropna().reset_index(
            drop=True)

        return data_results[
            ~data_results.EXPTYPE.str.contains('ACQ|DARK|FLAT')]


def get_program_ids(pid_file):
    """Retrieve the program IDs from the given text file."""
    programs_df = pd.read_csv(pid_file, delim_whitespace=True)
    all_programs = []
    for col, col_data in programs_df.iteritems():
        all_programs += col_data.to_numpy(dtype=str).tolist()

    return all_programs


class DarkDataModel(BaseDataModel):
    """DataModel for dark corrtag files."""
    cosmo_layout = False

    def get_new_data(self):
        """Set the model for what data is to be retrieved from each dark
        file."""
        # this way when you get new data it will get all the data
        header_request = {
            0: ['ROOTNAME', 'SEGMENT'], 1: ['EXPTIME', 'EXPSTART']
            }
        table_request = {
            1: ['PHA', 'XCORR', 'YCORR', 'TIME'],
            3: ['TIME', 'LATITUDE', 'LONGITUDE']
            }

        files = []

        program_ids = get_program_ids(PROGRAMS)

        for prog_id in program_ids:
            new_files_source = os.path.join(FILES_SOURCE, prog_id)
            files += find_files('*corrtag*', data_dir=new_files_source)

        if self.model is not None:
            currently_ingested = [item.FILENAME for item in
                                  self.model.select(self.model.FILENAME)]

            for file in currently_ingested:
                files.remove(file)

        if not files:  # No new files
            return pd.DataFrame()

        data_results = data_from_exposures(files,
                                           header_request=header_request,
                                           table_request=table_request)

        return data_results

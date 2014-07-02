'''
MPI script for infilling missing values in 
incomplete Tmin/Tmax station time series.

Must be run using mpiexec or mpirun.
'''

import numpy as np
from mpi4py import MPI
import sys
from twx.db import StationDataDb, STN_ID, MEAN_TMIN,\
MEAN_TMAX, NNRNghData, create_quick_db
from twx.utils import StatusCheck, Unbuffered
from netCDF4 import Dataset
import netCDF4
from twx.infill import InfillMatrixPPCA
import os

TAG_DOWORK = 1
TAG_STOPWORK = 2
TAG_OBSMASKS = 3

RANK_COORD = 0
RANK_WRITE = 1
N_NON_WRKRS = 2

P_PATH_DB = 'P_PATH_DB'
P_PATH_OUT = 'P_PATH_OUT'
P_PATH_NNR = 'P_PATH_NNR'

P_START_YMD = 'P_START_YMD'
P_END_YMD = 'P_END_YMD'
P_NCDF_MODE = 'P_NCDF_MODE'
P_STNIDS_TMIN = 'P_STNIDS_TMIN'
P_STNIDS_TMAX = 'P_STNIDS_TMAX'

P_MIN_NNGH_DAILY = 'P_MIN_NNGH_DAILY'
P_NNGH_NNR = 'P_NNGH_NNR'
P_NNR_VARYEXPLAIN = 'P_NNR_VARYEXPLAIN'
P_FRACOBS_INIT_PCS = 'P_FRACOBS_INIT_PCS'
P_PPCA_VARYEXPLAIN = 'P_PPCA_VARYEXPLAIN'
P_CHCK_IMP_PERF = 'P_CHCK_IMP_PERF'
P_NPCS_PPCA = 'P_NPCS_PPCA'
P_VERBOSE = 'P_VERBOSE'

LAST_VAR_WRITTEN = 'mae'

# rpy2
import rpy2
import rpy2.robjects as robjects
from rpy2.robjects.numpy2ri import numpy2ri
robjects.conversion.py2ri = numpy2ri
r = robjects.r

sys.stdout = Unbuffered(sys.stdout)

def proc_work(params, rank):

    status = MPI.Status()

    stn_da = StationDataDb(params[P_PATH_DB], (params[P_START_YMD], params[P_END_YMD]))
    days = stn_da.days
    ndays = float(days.size)

    empty_fill = np.ones(ndays, dtype=np.float32) * netCDF4.default_fillvals['f4']
    empty_flags = np.ones(ndays, dtype=np.int8) * netCDF4.default_fillvals['i1']
    empty_bias = netCDF4.default_fillvals['f4']
    empty_mae = netCDF4.default_fillvals['f4']

    ds_nnr = NNRNghData(params[P_PATH_NNR], (params[P_START_YMD], params[P_END_YMD]))

    bcast_msg = None
    bcast_msg = MPI.COMM_WORLD.bcast(bcast_msg, root=RANK_COORD)
    stnids_tmin, stnids_tmax = bcast_msg
    print "".join(["WORKER ", str(rank), ": Received broadcast msg"])

    while 1:

        stn_id = MPI.COMM_WORLD.recv(source=RANK_COORD, tag=MPI.ANY_TAG, status=status)

        if status.tag == TAG_STOPWORK:
            MPI.COMM_WORLD.send([None] * 7, dest=RANK_WRITE, tag=TAG_STOPWORK)
            print "".join(["WORKER ", str(rank), ": Finished"])
            return 0
        else:

            try:
                run_infill_tmin = stn_id in stnids_tmin
                run_infill_tmax = stn_id in stnids_tmax

                if run_infill_tmin:
                    a_pca_matrix = InfillMatrixPPCA(stn_id, stn_da, 'tmin', ds_nnr)
                    fnl_tmin, fill_mask_tmin, infill_tmin, mae_tmin, bias_tmin = infill_tair(a_pca_matrix, params)

                if run_infill_tmax:
                    a_pca_matrix = InfillMatrixPPCA(stn_id, stn_da, 'tmax', ds_nnr)
                    fnl_tmax, fill_mask_tmax, infill_tmax, mae_tmax, bias_tmax = infill_tair(a_pca_matrix, params)

            except Exception as e:

                print "".join(["ERROR: Could not infill ", stn_id, "|", str(e)])
                if run_infill_tmin:
                    fnl_tmin, fill_mask_tmin, infill_tmin, mae_tmin, bias_tmin = empty_fill, empty_flags, empty_fill, empty_mae, empty_bias
                if run_infill_tmax:
                    fnl_tmax, fill_mask_tmax, infill_tmax, mae_tmax, bias_tmax = empty_fill, empty_flags, empty_fill, empty_mae, empty_bias

            if run_infill_tmin:
                MPI.COMM_WORLD.send((stn_id, 'tmin', fnl_tmin, fill_mask_tmin, infill_tmin, mae_tmin, bias_tmin), dest=RANK_WRITE, tag=TAG_DOWORK)
            if run_infill_tmax:
                MPI.COMM_WORLD.send((stn_id, 'tmax', fnl_tmax, fill_mask_tmax, infill_tmax, mae_tmax, bias_tmax), dest=RANK_WRITE, tag=TAG_DOWORK)
            MPI.COMM_WORLD.send(rank, dest=RANK_COORD, tag=TAG_DOWORK)

def proc_write(params, nwrkers):

    status = MPI.Status()
    stn_da = StationDataDb(params[P_PATH_DB], (params[P_START_YMD], params[P_END_YMD]))
    days = stn_da.days
    nwrkrs_done = 0

    bcast_msg = None
    bcast_msg = MPI.COMM_WORLD.bcast(bcast_msg, root=RANK_COORD)
    stnids_tmin, stnids_tmax = bcast_msg
    print "WRITER: Received broadcast msg"

    path_out_tmin = os.path.join(params[P_PATH_OUT], 'infill_tmin.nc')
    path_out_tmax = os.path.join(params[P_PATH_OUT], 'infill_tmax.nc')

    if params[P_NCDF_MODE] == 'r+':

        ds_tmin = Dataset(path_out_tmin, 'r+')
        ds_tmax = Dataset(path_out_tmax, 'r+')
        ttl_infills = stnids_tmin.size + stnids_tmax.size
        stnids_tmin = np.array(ds_tmin.variables['stn_id'][:], dtype="<S16")
        stnids_tmax = np.array(ds_tmax.variables['stn_id'][:], dtype="<S16")

    else:

        stns_tmin = stn_da.stns[np.in1d(stn_da.stns[STN_ID], stnids_tmin, assume_unique=True)]
        variables_tmin = [('tmin', 'f4', netCDF4.default_fillvals['f4'], 'minimum air temperature', 'C'),
                          ('flag_infilled', 'i1', netCDF4.default_fillvals['i1'], 'infilled flag', ''),
                          ('tmin_infilled', 'f4', netCDF4.default_fillvals['f4'], 'infilled minimum air temperature', 'C')]
        create_quick_db(path_out_tmin, stns_tmin, days, variables_tmin)
        stnda_out_tmin = StationDataDb(path_out_tmin, mode="r+")
        stnda_out_tmin.add_stn_variable('mae', 'mean absolute error', 'C', "f8")
        stnda_out_tmin.add_stn_variable('bias', 'bias', 'C', "f8")
        ds_tmin = stnda_out_tmin.ds

        stns_tmax = stn_da.stns[np.in1d(stn_da.stns[STN_ID], stnids_tmax, assume_unique=True)]
        variables_tmax = [('tmax', 'f4', netCDF4.default_fillvals['f4'], 'maximum air temperature', 'C'),
                          ('flag_infilled', 'i1', netCDF4.default_fillvals['i1'], 'infilled flag', ''),
                          ('tmax_infilled', 'f4', netCDF4.default_fillvals['f4'], 'infilled maximum air temperature', 'C')]
        create_quick_db(path_out_tmax, stns_tmax, days, variables_tmax)
        stnda_out_tmax = StationDataDb(path_out_tmax, mode="r+")
        stnda_out_tmax.add_stn_variable('mae', 'mean absolute error', 'C', "f8")
        stnda_out_tmax.add_stn_variable('bias', 'bias', 'C', "f8")
        ds_tmax = stnda_out_tmax.ds

        ttl_infills = stnids_tmin.size + stnids_tmax.size

    print "WRITER: Infilling a total of %d station time series " % (ttl_infills,)
    print "WRITER: Output NCDF files ready"

    stat_chk = StatusCheck(ttl_infills, 10)

    while 1:

        stn_id, tair_var, tair, fill_mask, tair_infill, mae, bias = MPI.COMM_WORLD.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)

        if status.tag == TAG_STOPWORK:

            nwrkrs_done += 1
            if nwrkrs_done == nwrkers:

                print "Writer: Finished"
                return 0
        else:

            if tair_var == 'tmin':
                stn_idx = np.nonzero(stnids_tmin == stn_id)[0][0]
                ds = ds_tmin
            else:
                stn_idx = np.nonzero(stnids_tmax == stn_id)[0][0]
                ds = ds_tmax

            ds.variables[tair_var][:, stn_idx] = tair
            ds.variables["".join([tair_var, "_infilled"])][:, stn_idx] = tair_infill
            ds.variables['flag_infilled'][:, stn_idx] = fill_mask
            ds.variables['bias'][stn_idx] = bias
            ds.variables[LAST_VAR_WRITTEN][stn_idx] = mae

            ds.sync()

            print "|".join(["WRITER", stn_id, tair_var, "%.4f" % (mae,), "%.4f" % (bias,)])

            stat_chk.increment()

def proc_coord(params, nwrkers):

    stn_da = StationDataDb(params[P_PATH_DB], (params[P_START_YMD], params[P_END_YMD]))

    mask_tmin = np.isfinite(stn_da.stns[MEAN_TMIN])
    mask_tmax = np.isfinite(stn_da.stns[MEAN_TMAX])

    stnids_tmin = stn_da.stn_ids[mask_tmin]
    stnids_tmax = stn_da.stn_ids[mask_tmax]

    # Check if we're restarting a run
    if params[P_NCDF_MODE] == 'r+':

        # If rerunning remove stn ids that have already been completed
        try:

            if params[P_STNIDS_TMIN] == None:

                ds_tmin = Dataset(os.path.join(params[P_PATH_OUT], 'infill_tmin.nc'))
                mask_incplt = ds_tmin.variables[LAST_VAR_WRITTEN][:].mask
                stnids_tmin = stnids_tmin[mask_incplt]

            else:

                stnids_tmin = params[P_STNIDS_TMIN]

        except AttributeError:
            # no mask: infill complete
            stnids_tmin = np.array([], dtype="<S16")

        try:

            if params[P_STNIDS_TMAX] == None:

                ds_tmax = Dataset(os.path.join(params[P_PATH_OUT], 'infill_tmax.nc'))
                mask_incplt = ds_tmax.variables[LAST_VAR_WRITTEN][:].mask
                stnids_tmax = stnids_tmax[mask_incplt]

            else:

                stnids_tmax = params[P_STNIDS_TMAX]

        except AttributeError:
            # no mask: infill complete
            stnids_tmax = np.array([], dtype="<S16")


    stnids_all = np.unique(np.concatenate((stnids_tmin, stnids_tmax)))

    # Send stn ids to all processes
    MPI.COMM_WORLD.bcast((stnids_tmin, stnids_tmax), root=RANK_COORD)

    print "COORD: Done initialization. Starting to send work."

    cnt = 0
    nrec = 0

    for stn_id in stnids_all:

        if cnt < nwrkers:
            dest = cnt + N_NON_WRKRS
        else:
            dest = MPI.COMM_WORLD.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG)
            nrec += 1

        MPI.COMM_WORLD.send(stn_id, dest=dest, tag=TAG_DOWORK)
        cnt += 1

    for w in np.arange(nwrkers):
        MPI.COMM_WORLD.send(None, dest=w + N_NON_WRKRS, tag=TAG_STOPWORK)

    print "COORD: done"

def infill_tair(a_pca_matrix, params):


    fnl_tair, mask_infill, infill_tair = a_pca_matrix.infill(min_daily_nnghs=params[P_MIN_NNGH_DAILY],
                                                                nnghs_nnr=params[P_NNGH_NNR],
                                                                max_nnr_var=params[P_NNR_VARYEXPLAIN],
                                                                chk_perf=params[P_CHCK_IMP_PERF],
                                                                npcs=params[P_NPCS_PPCA],
                                                                frac_obs_initnpcs=params[P_FRACOBS_INIT_PCS],
                                                                ppca_varyexplain=params[P_PPCA_VARYEXPLAIN],
                                                                verbose=params[P_VERBOSE])

    # Calculate MAE/bias on days with both observed and infilled values
    obs_mask = np.logical_not(mask_infill)
    difs = infill_tair[obs_mask] - fnl_tair[obs_mask]
    mae = np.mean(np.abs(difs))
    bias = np.mean(difs)

    return fnl_tair, mask_infill, infill_tair, mae, bias


if __name__ == '__main__':

    PROJECT_ROOT = "/projects/topowx"
    FPATH_STNDATA = os.path.join(PROJECT_ROOT, 'station_data')

    np.seterr(all='raise')
    np.seterr(under='ignore')

    rank = MPI.COMM_WORLD.Get_rank()
    nsize = MPI.COMM_WORLD.Get_size()

    params = {}
    params[P_PATH_DB] = '/projects/topowx/refactor_test/all/tair_homog_1948_2012.nc'
    params[P_PATH_OUT] = '/projects/topowx/refactor_test/infill'
    #params[P_PATH_DB] = os.path.join(FPATH_STNDATA, 'all', 'tair_homog_1948_2012.nc')
    #params[P_PATH_OUT] = os.path.join(FPATH_STNDATA, 'infill')
    
    params[P_PATH_NNR] = os.path.join(PROJECT_ROOT, 'reanalysis_data', 'conus_subset')
    params[P_NCDF_MODE] = 'w'  # w or r+
    params[P_START_YMD] = 19480101
    params[P_END_YMD] = 20121231

    # PPCA parameters for infilling
    params[P_MIN_NNGH_DAILY] = 3
    params[P_NNGH_NNR] = 4
    params[P_NNR_VARYEXPLAIN] = 0.99
    params[P_FRACOBS_INIT_PCS] = 0.5
    params[P_PPCA_VARYEXPLAIN] = 0.99
    params[P_CHCK_IMP_PERF] = True
    params[P_NPCS_PPCA] = 0
    params[P_VERBOSE] = False

    # Set to arrays of station ids if only want to infill
    # a certain set of stations
    params[P_STNIDS_TMIN] = None
    params[P_STNIDS_TMAX] = None

    if rank == RANK_COORD:
        proc_coord(params, nsize - N_NON_WRKRS)
    elif rank == RANK_WRITE:
        proc_write(params, nsize - N_NON_WRKRS)
    else:
        proc_work(params, rank)

    MPI.COMM_WORLD.Barrier()
'''
Script to download SNOTEL/SCAN station data from NRCS.
'''

import obsio
import os
from twx.utils import TwxConfig
import ssl


if __name__ == '__main__':
    ssl._create_default_https_context = ssl._create_unverified_context
    twx_cfg = TwxConfig(os.getenv('TOPOWX_INI'))
    
    # Bounding box for stations
    bbox = obsio.BBox(*twx_cfg.stn_bbox)
        
    obsiof = obsio.ObsIoFactory(twx_cfg.obs_elems, bbox, twx_cfg.obs_start_date,
                                twx_cfg.obs_end_date)
    nrcs = obsiof.create_obsio_dly_nrcs()
    
    print(nrcs.stns.station_id)
    # Download observations to PyTables HDF file
    nrcs.to_hdf(fpath=twx_cfg.fpath_stndata_hdf_snotel,
                 stn_ids=nrcs.stns.station_id, chk_rw=twx_cfg.stn_read_chunk_snotel,
                 verbose=True, complevel=5, complib='zlib')

    print("END")

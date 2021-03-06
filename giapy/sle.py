"""
giasim.py
Author: Samuel B. Kachuck
Date  : 08 01 2015

    Main module for the giapy package. It provides the class GiaSimGlobal,
    which contains the method for computing the glacial isostatic adjustment to
    an earth with a given rheological decay spectrum to an ice history model.

Methods
-------
configure_giasim

Classes
-------
GiaSimGlobal
GiaSimOutput

"""
from __future__ import division

import numpy as np
import spharm
import subprocess
try:
    from progressbar import ProgressBar, Percentage, Bar, ETA
except:
    pass

from giapy.map_tools import GridObject, sealevelChangeByMelt,\
                    volumeChangeLoad, sealevelChangeByUplift, oceanUpliftLoad,\
                    floatingIceRedistribute

from giapy import GITVERSION, timestamp, MODPATH, call, os

class GiaSimGlobal(object):
    def __init__(self, earth, ice, grid=None, topo=None):
        """
        Compute glacial isostatic adjustment on a globe.

        Paramaters
        ----------
        earth : <giapy.code.earth_tools.earthSpherical.SphericalEarth>
        ice   : <giapy.code.icehistory.IceHistory / PersistentIceHistory>
        grid  : <giapy.code.map_tools.GridObject>
        topo  : numpy.ndarray

        Methods
        -------
        performConvolution
            

        """

        self.earth = earth

        self.ice = ice
        self.nlat, self.nlon = ice.shape
        
        # The grid used is a cylindrical projection (equispaced lat/lon grid
        # unless otherwise specified)
        if grid is not None:
            self.grid = grid
        else:
            self.grid = GridObject(mapparam={'projection': 'cyl'}, 
                                    shape=ice.shape)

        self.topo = topo
        
        # Precompute and store harmonic transform coefficients, for
        # computational efficiency, but at a memory cost.
        self.harmTrans = spharm.Spharmt(self.nlon, self.nlat, legfunc='stored')

    def performConvolution(self, out_times=None, ntrunc=None, topo=None,
                            verbose=False, eliter=5, nrem=1, massconerr=1e-2):  
        """Convolve an ice load and an earth response model in fft space.
        Calculate the uplift associated with stored earth and ice model.
        
        Parameters
        ----------
        out_times : an array of times at which to caluclate the convolution.
                    (default is to use previously stored values).
        ntrunc : int
           The truncation number of the spherical harmonic expansion. Default
           is from the earth model, must be <= ice model's and < grid.nlat
        topo   : array
            Topography on which to compute. If None (default), assumes a flat
            topography. Must be the same shapt as ice.
        verbose : boolean
            Display progress on computation. Depends on progressbar module.
            Default is False.
        eliter  : int
            The maximum number of iterations allowed to compute initial elastic
            response to redistributed load at each stage. If 0, instantaneous
            elastic response is not computed. Default 5.
        nrem   : int
            Number of removal stages between the provided ice stages
            (intermediate steps are interpolated linearly). Default 1.
       
        Results
        -------
        observerDict : GiaSimOutput
            A dictionary whose keys are fields of interest, such as uplift
            ('upl'), geoid ('geo'), and solid surface topography ('sstopo'),
            computed on the input grid at out_times.
        """
 
        DENICE      = 931.   #934.           # kg/m^3
        DENWAT      = 1000.  #999.           # kg/m^3
        DENSEA      = 1000.  #1029.          # kg/m^3
        GSURF       = 9.815          # m/s^2
        #PAperM      = DENSEA*GSURF
        NREM        = nrem           # number of intermediate steps


        earth = self.earth
        ice = self.ice
        grid = self.grid
        if topo is None and self.topo is not None:
            topo = self.topo
        if topo is not None:
            assert topo.shape == ice.shape, 'Topo and Ice must have the same shape'

        # Resolution
        ntrunc = ntrunc or min(earth.nmax, ice.nlat-1)
        assert ntrunc <= ice.nlat-1, 'ntrunc > ice.nlat-1'
        ms, ns = spharm.getspecindx(ice.nlat-1)
        # npad is the indices in the larger (padded) array of spherical
        # harmonics that correspond to the smaller (response) array.
        npad = (ns <= ntrunc)
        
        # Store out_times
        if out_times is None:
            out_times = self.out_times
        else:
            out_times = out_times
        self.out_times = out_times
        assert out_times is not None, 'out_times is not set'
        
        # Calculate times of intermediate removal stages.
        diffs = np.diff(ice.times)
        addRemovalTimes = []
        for i in range(1, NREM+1):
            addRemovalTimes.append(ice.times[:-1]+i*diffs/NREM)
        addRemovalTimes = np.array(addRemovalTimes).flatten()
        remTimes = np.union1d(ice.times, addRemovalTimes)[::-1]
        calcTimes = np.union1d(remTimes, out_times)[::-1]

        # Initialize output observer         
        observerDict = initialize_output(self, out_times, calcTimes, ice.nlat-1, 
                                            ntrunc, ns, ice.shape) 

        for o in observerDict:
            o.loadStageUpdate(ice.times[0], sstopo=topo)

        esl = 0                 # Equivalent sea level assumed to start at 0.

        elRespArray = earth.getResp(0.)
        ssResp = np.zeros_like(ns) 
        ssResp[npad] = observerDict['SS'].isolateRespArray(elRespArray)

        # Convolve each ice stage to the each output time.
        # Primary loop: over ice load changes.
        for icea, ta, iceb, tb in ice.pairIter():
            ################### LOAD STAGE CALCULATION ###################
            # Determine the water load redistribution for ice, uplift, and
            # geoid changes between ta and tb,
            if topo is not None:
                # Get index for starting time.
                nta = observerDict['SS'].locateByTime(ta)
                # Collect the solid-surface topography at beginning of step.
                Ta = observerDict['sstopo'].array[nta]

                # Redistribute the ocean by change in ocean floor / surface.
                ssa, ssb = observerDict['SS'].array[[nta, nta+1]] 
                dSS = self.harmTrans.spectogrd(ssb-ssa)
                dhwBarU = sealevelChangeByUplift(dSS, Ta+DENICE/DENSEA*icea, 
                                                        grid)
                dhwU = oceanUpliftLoad(dhwBarU, Ta+DENICE/DENSEA*icea, dSS)

                # Update the solid-surface topography with uplift / geoid.
                Tb = Ta + dSS - dhwBarU
                esl += dhwBarU
                dLoad = dhwU.copy()
                dwLoad = dhwU.copy()                # Save the water load

                # Redistribute ice, consistent with current floating ice. 
                dILoad, dhwBarI = floatingIceRedistribute(icea, iceb, Tb, grid,
                                                            DENICE/DENSEA)

                # Combine loads from ocean changes and ice volume changes.
                dLoad += dILoad
                esl += dhwBarI
                dwLoad += volumeChangeLoad(dhwBarI, Tb+DENICE/DENSEA*iceb)
                Tb -= dhwBarI
                

                # Calculate instantaneous (elastic and gravity) responses to
                # the load shift and redistribute ocean accordingly.
                # Note: WE DO NOT CURRENTLY RECHECK FOR FLOATING ICE LOADS.
                if eliter:
                    # Get elastic and geoid response to the water load.
                    # Find the elastic uplift in response to stage's load
                    # redistribution.
                    dSSel = self.harmTrans.spectogrd((ssResp)*\
                                self.harmTrans.grdtospec(dLoad)) 

                    dhwBarUel = sealevelChangeByUplift(dSSel, 
                                                        Tb+DENICE/DENSEA*iceb, grid)
                    dhwUel = oceanUpliftLoad(dhwBarUel, 
                                                Tb+DENICE/DENSEA*iceb, dSSel)

                    Tb = Tb + dSSel - dhwBarUel
                    esl += dhwBarUel
                    dLoad = dLoad + dhwUel
                    dwLoad += dhwUel

                    # Iterate elastic responses until they are sufficiently small.
                    for i in range(eliter):
                        # Need to save elastic uplift and geoid at each iteration
                        # to compare to previous steps for convergence.
                        dSSelp = self.harmTrans.spectogrd((ssResp)*\
                                    self.harmTrans.grdtospec(dhwUel))
                      
                     

                        dhwBarUel = sealevelChangeByUplift(dSSelp, 
                                                            Tb+DENICE/DENSEA*iceb, grid)
                        dhwUel = oceanUpliftLoad(dhwBarUel, 
                                                    Tb+DENICE/DENSEA*iceb, dSSelp)

                        # Correct topography
                        Tb = Tb + dSSelp - dhwBarUel
                        esl += dhwBarUel
                        dLoad = dLoad + dhwUel
                        dwLoad += dhwUel

                        # Truncation error from further iteration
                        err = np.mean(np.abs(dSSelp))/np.mean(np.abs(dSSel))
                        if err <= massconerr:
                            break
                        else:
                            dSSel = dSSel + dSSelp
                       
                            continue

                observerDict['SS'].array[nta+1] += self.harmTrans.grdtospec(dSSel) 

                for o in observerDict:
                    # Topography and load for time tb are updated and saved.
                    o.loadStageUpdate(tb, dLoad=dLoad, 
                                      topo=Tb+iceb*(Tb + DENICE/DENSEA*iceb>=0), 
                                      esl=esl, dwLoad=dwLoad, sstopo=Tb)

            else:
                dLoad = (iceb-icea)*DENICE/DENSEA
                Tb = None

                for o in observerDict:
                    # Topography and load for time tb are updated and saved.
                    o.loadStageUpdate(tb, dLoad=dLoad)

            # Transform load change into spherical harmonics.
            loadChangeSpec = self.harmTrans.grdtospec(dLoad)/NREM
            
            # Check for mass conservation.
            massConCheck = np.abs(loadChangeSpec[0])/np.abs(loadChangeSpec.max())
            if  verbose and massConCheck >= massconerr:
                print("Load at {0} doesn't conserve mass: {1}.".format(ta,
                                                                massConCheck))
            # N.B. the n=0 load should be zero in cases of glacial isostasy, as 
            # mass is conserved during redistribution.

            ################# RESPONSE STAGE CALCULATION #################
            # Secondary loop: over output times.
            for inter_time in np.linspace(tb, ta, NREM, endpoint=False)[::-1]:
                # Perform the time convolution for each output time
                for t_out in calcTimes[calcTimes < inter_time]:
                    respArray = earth.getResp(inter_time-t_out)
                    for o in observerDict:
                        o.respStageUpdate(t_out, respArray, 
                                            DENSEA*loadChangeSpec) 

        # Don't keep the intermediate uplift stages for water redistribution
        #observerDict.removeObserver('eslUpl', 'eslGeo') 

        return observerDict

def configure_giasim(configdict=None):
    """
    Convenience function for setting up a GiaSimGlobal object.

    The function uses inputs stored in giapy/data/inputs/, which can be
    downloaded from pages.physics.cornell.edu/~skachuck/giainputs.tar.gz or
    using the script giapy/dldata.

    Parameters
    ----------
    configdict : dict
        A dictionary with keys 'earth' and 'ice' and values of the
        names of these inputs. The key 'topo' is optional, and defaults to a
        flat initial topography.

    sim        : GiaSimGlobal object
    """

    DEFAULTCONFIG = {'earth': '75km0p04Asth_4e23Lith',
                     'ice'  : 'AA2_Tail_nochange5_hightres_Pers_288_square',
                     'topo' : 'sstopo288'}

    
    configdict = configdict or DEFAULTCONFIG
    
    assert configdict.has_key('earth'), 'GiaSimGlobal needs earth specified'
    assert configdict.has_key('ice'), 'GiaSimGlobal needs ice specified'

    #ppath = os.path.dirname(os.path.split(__file__)[0])
    dpath = MODPATH + '/data/inputs/' 
    ename = configdict['earth']+'.earth'
    iname = configdict['ice']+'.ice'

    filecheck = os.path.isfile(dpath+ename) and os.path.isfile(dpath+iname)
    
    topo = configdict.get('topo', None)
    if topo is not None:
        tname = topo+'.topo'
        filecheck = filecheck and os.path.isfile(dpath+tname)
    
    if not filecheck:
        with open(MODPATH+'/data/inputfilelist', 'r') as f:
            filelist = f.read().splitlines()
        if ename in filelist and iname in filelist and tname in filelist:
            print('Downloading inputs')
            p = subprocess.Popen(os.path.join(MODPATH+'/dldata'), shell=True,
                                cwd=ppath)
            p.wait()
        else:
            raise IOError('One of the inputs you specified does not exist.')

    earth = np.load(dpath+ename)
    ice = np.load(dpath+iname)
    if topo is not None:
        topo = np.load(dpath+tname)[2]
    
    sim = GiaSimGlobal(earth=earth, ice=ice, topo=topo)

    return sim

def initialize_output(sim, out_times, calcTimes, nmax, ntrunc, ns, shape):
    earth = sim.earth
    # Initialize the return object to include...
    # ... values desired at output times
    #   [1] Uplift
    uplObserver = earth.TotalUpliftObserver(out_times, nmax, ntrunc, ns)
    #   [2] Horizontal deformation
    horObserver = earth.TotalHorizontalObserver(out_times, nmax, ntrunc, ns)
    #   [3] Uplift velocities
    velObserver = earth.VelObserver(out_times, nmax, ntrunc, ns)
    #   [4] Geoid perturbations
    geoObserver = earth.GeoidObserver(out_times, nmax, ntrunc, ns)
    #   [5] Gravitational acceleration perturbations
    gravObserver = earth.GravObserver(out_times, nmax, ntrunc, ns) 

    # ... and values needed to perform the convolution
    #   [1] Uplift for ocean redistribution
    SeaSurfaceObserver = earth.SeaSurfaceObserver(calcTimes, nmax, ntrunc, ns)
    #   [2] Geoid for ocean redistribution
    eslGeoObserver = earth.GeoidObserver(calcTimes, nmax, ntrunc, ns)
    #   [3] Topography (to top of ice) to find floating ice
    topoObserver = HeightObserver(calcTimes, shape, 'topo')
    #   [4] Load (total water + ice load in water equivalent)
    loadObserver = HeightObserver(calcTimes, shape, 'dLoad')
    #   [5] Water load
    wloadObserver = HeightObserver(calcTimes, shape, 'dwLoad') 
    #   [6] Solid surface topography for ocean redistribution
    rslObserver = HeightObserver(calcTimes, shape, 'sstopo')
    #   [7] Eustatic sea level, with average uplift and geoid over oceans.
    eslObserver = EslObserver(calcTimes) 

    observerDict = GiaSimOutput(sim)
    observerDict.addObserver('upl'   , uplObserver)
    observerDict.addObserver('hor'   , horObserver)
    observerDict.addObserver('grav'  , gravObserver)
    observerDict.addObserver('load'  , loadObserver)
    observerDict.addObserver('wload' , wloadObserver)
    observerDict.addObserver('vel'   , velObserver)
    observerDict.addObserver('geo'   , geoObserver)

    observerDict.addObserver('SS', SeaSurfaceObserver) 
    observerDict.addObserver('topo'  , topoObserver)
    observerDict.addObserver('esl'   , eslObserver)
    observerDict.addObserver('sstopo', rslObserver)
    return observerDict

class GiaSimOutput(object):
    """A container object for computations of glacial isostatic adjustment.

    The results of the GIA computation can be accessed via object attributes or
    via a dictionary (e.g., if iteration is desired). The object's __repr__
    gives the computed attributes.

    Parameters
    ----------
    inputs : anything
        anything desired to be stored as inputs. Typically, this will be a
        GiaSimGlobal object that contains the ice load, earth model, and other
        necessary objects for reproducing the computation.

    Methods
    -------
    addObserver - add an observer to the watchlist
    removeObserver - remove an observer from the watchlist
    transformObservers - transform each observer in the watchlist, using each's
        own transform function (must be provided by observer).


    Data
    ----
    GITVERSION : the hash of the current giapy git HEAD
    TIMESTAMP : the datetime of creation (calculation)
    """
    def __init__(self, inputs):
        self.GITVERSION = GITVERSION
        self.TIMESTAMP = timestamp()
        self.inputs = inputs
        self._observerDict = {}

    def __getitem__(self, key):
        return self._observerDict.__getitem__(key)

    def __iter__(self):
        return self._observerDict.itervalues() 

    def __repr__(self):
        retstr = ''
        retstr = 'GIA computed on {}\n'.format(self.TIMESTAMP)
        retstr += 'Has observers: '+', '.join(self._observerDict)
        return retstr

    def addObserver(self, name, observer):
        self._observerDict[name] = observer
        setattr(self, name, observer)

    def removeObserver(self, *names):
        for name in names:
            del self._observerDict[name]
            delattr(self, name)

    def transformObservers(self, inverse=False):
        for obs in self:
            obs.transform(self.inputs.harmTrans, inverse=inverse)


class AbstractGiaSimObserver(object):
    """The GiaSimObserver mediates between an earth model's response function
    and the convolved result. It needs to store the harmonic response

    initialize and update must be overwritten for class to work, all others 
    simply pass.
    """
    def __init__(self):
        pass

    def __getitem__(self, key):
        return self.array.__getitem__(key)
        
    def __iter__(self):
        return self.array.__iter__()
    
    @property
    def shape(self):
        return self.array.shape

    def initialize(self, outTimes, ntrunc):
        raise NotImplemented

    def loadStageUpdate(self, *args, **kwargs):
        pass

    def respStageUpdate(self, *args, **kwargs):
        pass

    def update(self, tout, tdur, dLoad):
        raise NotImplemented

    def transform(self, trans, inverse=True):
        pass

    def locateByTime(self, time):
        if time not in self.outTimes:
            raise ValueError('time not in self.outTimes')
        return np.argwhere(self.outTimes == time)[0][0]

    def nearest_to(self, time):
        """Return a field from outTimes nearest to time.
        """
        idx = (np.abs(self.outTimes-time)).argmin()
        return self[idx]


class AbstractEarthGiaSimObserver(AbstractGiaSimObserver):
    """Abstract class for results in spherical harmonic space, updated during
    the response stage.

    Must implement isolateRespArray to pull proper response curve from the
    computed earth model.
    """
    def __init__(self, outTimes, nmax, ntrunc, ns):
        self.initialize(outTimes, nmax, ns)
        self.npad = (ns <= ntrunc)
        self.ns = ns[self.npad]
        self.spectral = True

    def initialize(self, outTimes, ntrunc, ns):
        self.array = np.zeros((len(outTimes), 
                               int((ntrunc+1)*(ntrunc+2)/2)), dtype=complex)
        self.outTimes = outTimes
        self.ns = ns

    def respStageUpdate(self, *args, **kwargs):
        self.update(*args, **kwargs)

    def update(self, tout, respArray, dLoad):
        if tout not in self.outTimes:
            return
        n = self.locateByTime(tout)
        resp = self.isolateRespArray(respArray)
        self.array[n][self.npad] += resp * dLoad[self.npad]

    def transform(self, trans, inverse=True):
        if not inverse and self.spectral:
            self.array = trans.spectogrd(self.array.T).T
            self.spectral = False
        elif inverse and not self.spectral:
            self.array = trans.grdtospec(self.array.T).T
            self.spectral = True

    def isolateRespArray(self, respArray):
        raise NotImplemented()


class HeightObserver(AbstractGiaSimObserver):
    """General observer for heights computed on the real-space grid and updated
    during the loadStage.
    """
    def __init__(self, outTimes, iceShape, name):
        self.initialize(outTimes, iceShape)
        self.name = name

    def initialize(self, outTimes, iceShape):
        self.array = np.zeros((len(outTimes), 
                                iceShape[0], iceShape[1]))
        self.outTimes = outTimes

    def loadStageUpdate(self, tout, **kwargs):
        if self.name in kwargs.keys():
            self.update(tout, kwargs[self.name])

    def update(self, tout, load):
        if tout not in self.outTimes:
            return
        n = self.locateByTime(tout)
        self.array[n] = load

class EslObserver(AbstractGiaSimObserver):
    def __init__(self, outTimes):
        self.array = np.zeros(len(outTimes))
        self.outTimes = outTimes

    def loadStageUpdate(self, tout, **kwargs):
        if 'esl' in kwargs.keys():
            self.update(tout, kwargs['esl'])

    def update(self, tout, esl):
        if tout not in self.outTimes:
            return
        n = self.locateByTime(tout)
        self.array[n] = esl


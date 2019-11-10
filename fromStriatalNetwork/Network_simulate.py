#
# This code reads the network created by Network_connect.py and set it
# up in memory
#
# mpiexec -n 4 python Network_simulate.py
#
#
# This open source software code was developed in part or in whole in
# the Human Brain Project, funded from the European Union’s Horizon
# 2020 Framework Programme for Research and Innovation under Specific
# Grant Agreements No. 720270 and No. 785907 (Human Brain Project SGA1
# and SGA2).


# !!!! GAP JUNCTION CODE NEEDS TO BE UPDATED -- currently disabled

#
############################################################################

from mpi4py import MPI # This must be imported before neuron, to run parallel
from neuron import h, gui
import h5py
import json

import bluepyopt.ephys as ephys
from Neuron_model_extended import NeuronModel
import pickle
from Network_place_neurons import NetworkPlaceNeurons
import numpy as np
from NrnSimulatorParallel import NrnSimulatorParallel

from glob import glob
import re
import os



##############################################################################

class NetworkSimulate():

  def __init__(self, network_info_file, inputFile=None,
               verbose=True, logFile=None, \
               disableGapJunctions=True,
               disableSynapses=False):

    self.logFile = logFile
    
    self.network_info_file = network_info_file
    self.inputFile = inputFile

    # !!! What value to use for synaptic weight and synapse delay?
    # !!! different for AMPA and GABA?
    self.synapseWeight = 10.0 # microsiemens 
    self.synapseDelay = 1      # ms 
    self.spikeThreshold = 10
    self.axonSpeed = 5.0 # 5m/s  --- ref?
    
    # list for holding current injections
    self.istim = []
    
    # holder for modulation transients
    self.modTrans = []

    self.disableGapJunctions = disableGapJunctions
    
    self.synapseTypeLookup = { 1 : "GABA", 2: "AMPA_NMDA", 3: "GapJunction" }
    
    self.neurons = {}
    self.sim = None
    self.neuronNodes = []

    self.virtualNeurons = {}
    
    self.netConList = [] # Avoid premature garbage collection
    self.synapseList = []
    self.gapJunctionList = []
    self.externalStim = dict([])
    self.tSave = []
    self.vSave = []
    self.vKey = []

    self.inputData = None
    
    self.gapJunctionNextGid = 0 # Are these gids separate from cell gids?
    
    self.tSpikes = h.Vector()
    self.idSpikes = h.Vector()
    
    self.verbose = verbose

    # Make sure the output dir exists, so we dont fail at end because we
    # cant write file
    self.createDir("save/traces")
    
    self.pc = h.ParallelContext()

    # self.writeLog("I am node " + str(int(self.pc.id())))

    
    # We need to initialise random streams, see Lytton el at 2016 (p2072)
    
    self.loadNetworkInfo(network_info_file)

    self.distributeNeurons()
    self.setupNeurons()
    self.pc.barrier()

#    for i in range(0,self.nNeurons):
#      print("Node : " + str(int(self.pc.id())) + " cell " + str(i) + " status " + str(self.pc.gid_exists(i)))

      
    self.connectNetwork(disableSynapses=disableSynapses)
    self.pc.barrier()
    
    # Do we need blocking call here, to make sure all neurons are setup
    # before we try and connect them

    

    # READ ABOUT PARALLEL NEURON
# https://www.neuron.yale.edu/neuron/static/new_doc/modelspec/programmatic/network/parcon.html#paralleltransfer

  ############################################################################

  def loadNetworkInfo(self, network_info_file, config_file=None):

    self.network_info_file = network_info_file

    # We need to check if a split network file exists
    splitFile = network_info_file.replace('save/','save/TEMP/').replace('.hdf5', '-%d.hdf5') % int(self.pc.id())
      
    import os.path
    if(os.path.isfile(splitFile)):
      network_info_file = splitFile
    else:
      self.writeLog("Unable to find " + splitFile + " using " \
                    + network_info_file)
    
    self.writeLog("Worker " + str(int(self.pc.id())) \
                  + ": Loading network from " + network_info_file)

    from Network_load import NetworkLoad
    self.network_info = NetworkLoad(network_info_file).data
    
    self.synapses = self.network_info["synapses"]
    self.gapJunctions = self.network_info["gapJunctions"]
    
    # We are only passed information about neurons on our node if
    # SplitConnectionFile was run, so need to use nNeurons to know
    # how many neurons in total
    self.nNeurons = self.network_info["nNeurons"]
      
    if(config_file is None):
      config_file = self.network_info["configFile"]

    self.config_file = config_file
    self.writeLog("Loading config file " + config_file)
      
    # Add checks to see that config file and network_info_file matches

    import json
    with open(config_file,'r') as config_file:
      self.config = json.load(config_file)

    # I do not know if the gap junction GIDs are a separate entity from the
    # neuron cell GIDs, so to be on safe side, let's make sure they
    # do not overlap
    self.gapJunctionNextGid = self.nNeurons + 100000000

    # Make a bool array indicating if cells are virtual or not    
    self.isVirtualNeuron = [n["virtualNeuron"] \
                            for n in self.network_info["neurons"]]

        
      

  ############################################################################
      
  def distributeNeurons(self):
    # This code is run on all workers, will generate different lists on each
    self.writeLog("Distributing neurons.")
    
    self.neuronID = range(int(self.pc.id()),self.nNeurons,int(self.pc.nhost()))

    self.neuronNodes = [x%int(self.pc.nhost()) for x in range(0,self.nNeurons)]

    if(False):
      self.writeLog("Node " + str(int(self.pc.id())) + " handling neurons: " \
                    + ' '.join(map(str, self.neuronID)))
    
  ############################################################################
  
  def destroy(self):
    for neuron in self.neurons:
      neuron.destroy(sim=self.sim)
                          
  ############################################################################

  # This requires self.sim to be defined
  
  def loadSynapseParameters(self):

    # We need to load all the synapse parameters
    self.synapseParameters = dict([])
    
    for (preType,postType) in self.network_info["connectivityDistributions"]:

      synData = self.network_info["connectivityDistributions"][preType,postType]
      synapseTypeID = synData[2]
      infoDict = synData[5]

      if("parameterFile" in infoDict):
        parFile = infoDict["parameterFile"]
        parDataDict = json.load(open(parFile,'r'))

        # Save data as a list, we dont need the keys
        parData = []
        for pd in parDataDict:
          parData.append(parDataDict[pd])
        
      else:
        parData = None

      if("modFile" in infoDict):
        modFile = infoDict["modFile"]
        try:
          evalStr = "self.sim.neuron.h." + modFile
          channelModule = eval(evalStr)
        except:
          import traceback
          tstr = traceback.format_exc()
          print(tstr)
          import pdb
          pdb.set_trace()
  
      else:
        assert False, "No channel module specified for " \
          + str(preType) + "->" + str(postType) + " synapses"
        channelModule = None

      self.synapseParameters[synapseTypeID] = (channelModule,parData)    

    
  ############################################################################

  def setupNeurons(self):
    
    self.writeLog("Setup neurons")
    
    # self.sim = ephys.simulators.NrnSimulator(cvode_active=False)
    #self.sim = NrnSimulatorParallel()
    self.sim = NrnSimulatorParallel(cvode_active=False)

    # We need to load all the synapse parameters
    self.loadSynapseParameters()
    
    # The neurons this node is responsible for is in self.neuronID
    for ID in self.neuronID:
     
      name = self.network_info["neurons"][ID]["name"]

      config = self.config[name]
      morph = config["morphology"]
      param = config["parameters"]
      mech = config["mechanisms"]

      # Obs, neurons is a dictionary
      if(self.network_info["neurons"][ID]["virtualNeuron"]):

        if(self.inputData is None):
          self.writeLog("Using " + self.inputFile + " for virtual neurons")
          self.inputData = h5py.File(self.inputFile,'r')
        
          name = self.network_info["neurons"][ID]["name"]
          spikes = self.inputData["input"][ID]["activity"]["spikes"].value
        
        # Creating NEURON VecStim and vector
        # https://www.neuron.yale.edu/phpBB/viewtopic.php?t=3125
        vs = h.VecStim()
        v = h.Vector(spikes.size)
        v.from_python(spikes)
        vs.play(v)

        self.virtualNeurons[ID] = dict([])
        self.virtualNeurons[ID]["spikes"] = (v,vs,spikes)
        self.virtualNeurons[ID]["name"] = name

        self.pc.set_gid2node(ID, int(self.pc.id()))

        nc = h.NetCon(vs,None)
        self.pc.cell(ID,nc,1) # The 1 means broadcast spikes to other machines
       
      else:
        # A real neuron (not a virtual neuron that just provides input)

        self.neurons[ID] = NeuronModel(param_file=param,
                                       morph_file=morph,
                                       mech_file=mech,
                                       cell_name=name)
        
        # Register ID as belonging to this worker node
        self.pc.set_gid2node(ID, int(self.pc.id()))

        if(True or False):
          self.writeLog("Node " + str(int(self.pc.id())) + " - cell " \
                        + str(ID) + " " + name)
     
        # We need to instantiate the cell
        try:
          self.neurons[ID].instantiate(sim=self.sim)
        except:
          import traceback
          tstr = traceback.format_exc()
          print(tstr)
          import pdb
          pdb.set_trace()
          
          
        # !!! DIRTY FIX for
        # https://github.com/BlueBrain/BluePyOpt/blob/master/bluepyopt/ephys/morphologies.py
        # This is likely the offending line, that pushes a segment to the stack
        # --> sim.neuron.h.execute('create axon[2]', icell)
      
        self.writeLog("!!! Popping extra segment from neuron -- temp fix!")
        h.execute("pop_section()")
      
        # !!! END OF DIRTY FIX
      
        # !!! Connect a netcon and register it, taken from ballandstick's
        #     connect2target function
        nc = h.NetCon(self.neurons[ID].icell.axon[0](0.5)._ref_v,
                      None,
                      sec = self.neurons[ID].icell.axon[0])
        nc.threshold = 10
     
        self.pc.cell(ID,nc,1) # The 1 means broadcast spikes to other machines
        # self.pc.outputcell(ID) # -- not needed, cell was called with a 1
        # self.netConList.append(nc) -- Not needed according to Lytton et al 2016
      
        # Record all spikes
        self.pc.spike_record(ID,self.tSpikes,self.idSpikes)

  ############################################################################

  def connectNetwork(self, disableSynapses=False):

    self.pc.barrier()

    # Add synapses
    if not disableSynapses: self.connectNetworkSynapses()
    
    # Add gap junctions
    if(self.disableGapJunctions):
      self.writeLog("Gap junctions disabled.")
    else:
      self.writeLog("Adding gap junctions.")
      assert False, "This code should be updated"
      self.connectNetworkGapJunctions()
    

    self.pc.barrier()

  ############################################################################

  def connectNetworkSynapses(self):

    # This loops through all the synapses, and connects the relevant ones
    nextRow = 0
    # nextRowSet = [ fromRow, toRow ) -- ie range(fromRow,toRow)
    nextRowSet = self.findNextSynapseGroup(nextRow)

    while(nextRowSet is not None):

      # Add the synapses to the neuron
      self.connectNeuronSynapses(startRow=nextRowSet[0],endRow=nextRowSet[1])

      # Find the next group of synapses
      nextRow = nextRowSet[1] # 2nd number was not included in range
      nextRowSet = self.findNextSynapseGroup(nextRow)     

  ############################################################################

  # This function starts at nextRow, then returns all synapses onto
  # a neuron which is located on the worker
  
  def findNextSynapseGroup(self,nextRow=0,connectionType="synapses"):

    if(connectionType == "synapses"):
      synapses = self.synapses
    elif(connectionType == "gapjunctions"):
      synapses = self.gapJunctions
    else:
      self.writeLog("!!! findNextSynapseGroup: Unknown connectionType: " \
                    + connectionType)
      import pdb
      pdb.set_trace()
      
    nSynRows = synapses.shape[0]

    if(nextRow >= nSynRows):
      # No more synapses to get
      return None
    
    # The synapse matrix is sorted on destID, ascending order
    # We also assume that self.neuronID is sorted in ascending order

    startRow = None
    notOurID = None
    
    while(startRow is None):

      # What is the next destination ID
      nextID = synapses[nextRow,1]

      # Is the next ID ours?
      if(nextID in self.neuronID):
        foundRow = True
        startRow = nextRow
        ourID = nextID
        continue
      else:
        notOurID = nextID

      while(nextRow < nSynRows and\
            synapses[nextRow,1] == notOurID):
        nextRow += 1

      if(nextRow >= nSynRows):
        # No more synapses to get
        return None


    # Next find the last of the rows with this ID      
    endRow = startRow

    while(endRow < nSynRows \
          and synapses[endRow,1] == ourID):
      endRow += 1

    return (startRow,endRow)
      

  ############################################################################

  # Processing the range(startRow,endRow) (ie, to endRow-1)
  
  def connectNeuronSynapses(self,startRow,endRow):

    sourceIDs = self.synapses[startRow:endRow,0]
    destID = self.synapses[startRow,1]
    assert (self.synapses[startRow:endRow,1] == destID).all()

    # Double check mapping
    assert self.pc.gid2cell(destID) == self.neurons[destID].icell, \
      "GID mismatch: " + str(self.pc.gid2cell(destID)) \
      + " != " + str(self.neurons[destID].icell)

    synapseTypeID = self.synapses[startRow:endRow,6]
    axonDistance = self.synapses[startRow:endRow,7] # Obs in micrometers

    secID=self.synapses[startRow:endRow,9]
    dendSections = self.neurons[destID].mapIDtoCompartment(secID)
    secX = self.synapses[startRow:endRow,10]/1000.0 # Convert to number 0-1

    # conductances are stored in pS (because we use INTs),
    # Neuron wants it in microsiemens??!
    conductance = self.synapses[startRow:endRow,11]*1e-6
    parameterID = self.synapses[startRow:endRow,12]
    
    for (srcID,section,sectionX,sTypeID,axonDist,cond,pID) \
      in zip(sourceIDs,dendSections,secX,synapseTypeID,
             axonDistance,conductance,parameterID):

      try:
        # !!! 
        self.addSynapse(cellIDsource=srcID,
                        dendCompartment=section,
                        sectionDist=sectionX,
                        synapseTypeID=sTypeID,
                        axonDist=axonDist,
                        conductance=cond,
                        parameterID=pID)
      except:
        import traceback
        tstr = traceback.format_exc()
        self.writeLog(tstr)
        import pdb
        pdb.set_trace()
        
        
  ############################################################################

  # OBS!! The src and dest lists can be different length
  #
  # src are all the gap junctions where the source compartment are
  # on the local worker.
  # dest are the gap junctions where the dest compartment are on the
  # local worker
  # The same GJ might appear in both src and dest lists, but at different rows
  
  def findLocalGapJunction(self):

    # If the gap junction matrix is too large to fit in memory then
    # this will need to be optimised
    
    GJidxA = np.where([x in self.neuronID \
                       for x in self.gapJunctions[:,0]])[0]

    GJidxB = np.where([x in self.neuronID \
                       for x in self.gapJunctions[:,1]])[0]

    GJIDoffset = self.network_info["GJIDoffset"]
    GJGIDsrcA = GJIDoffset + 2*GJidxA
    GJGIDsrcB = GJIDoffset + 2*GJidxB+1

    GJGIDdestA = GJIDoffset + 2*GJidxA+1
    GJGIDdestB = GJIDoffset + 2*GJidxB

    neuronIDA = self.gapJunctions[GJidxA,0]
    neuronIDB = self.gapJunctions[GJidxB,1]

    segIDA = self.gapJunctions[GJidxA,2]
    segIDB = self.gapJunctions[GJidxB,3]

    compartmentA = [self.neurons[x].mapIDtoCompartment([y])[0] \
                    for (x,y) in zip(neuronIDA,segIDA)]
    compartmentB = [self.neurons[x].mapIDtoCompartment([y])[0] \
                    for (x,y) in zip(neuronIDB,segIDB)]
    
    segXA = self.gapJunctions[GJidxA,4]
    segXB = self.gapJunctions[GJidxB,5]

    condA = self.gapJunctions[GJidxA,10]
    condB = self.gapJunctions[GJidxB,10]

    # Merge the two lists together
    
    GJidx = np.concatenate([GJidxA,GJidxB])
    GJGIDsrc = np.concatenate([GJGIDsrcA,GJGIDsrcB])
    GJGIDdest = np.concatenate([GJGIDdestA,GJGIDdestB])
    neuronID = np.concatenate([neuronIDA,neuronIDB])
    segID = np.concatenate([segIDA,segIDB])
    compartment = np.concatenate([compartmentA,compartmentB])
    segX = np.concatenate([segXA,segXB])
    cond = np.concatenate([condA,condB])
    
    
    return (neuronID,compartment,segX,GJGIDsrc,GJGIDdest,cond)
    
  ############################################################################
  
  def connectNetworkGapJunctions(self):
    
    (neuronID,compartment,segX,GJGIDsrc,GJGIDdest,cond) \
     = self.findLocalGapJunctions()

    for (nID,comp,sX,GIDsrc,GIDdest,g) \
      in (neuronID,compartment,segX,GJGIDsrc,GJGIDdest,cond):
    
      self.addGapJunction(section=comp,
                          sectionDist=sX,
                          GIDsourceGJ=GIDsrc,
                          GIDdestGJ=GIDdest,
                          gGapJunction=cond)
        
  ############################################################################

  # !!! FIX THIS FUNCTION
  
  def connectNeuronGapJunctions(startRow,endRow):

    sourceIDs = self.gapJunctions[startRow:endRow,0]
    destID = self.gapJunctions[startRow,1]

    assert (self.gapJunctions[startRow:endRow,1] == destID).all()

    # Double check mapping
    assert self.pc.gid2cell(destID) == self.neurons[destID].icell, \
      "GID mismatch: " + str(self.pc.gid2cell(destID)) \
      + " != " + str(self.neurons[destID].icell)

    sourceSecID = self.gapJunctions[startRow:endRow,2]
    destSecID = self.gapJunctions[startRow:endRow,3]

    # !!! Double check we get number between 0.0 and 1.0
    sourceSecX = self.gapJunctions[startRow:endRow,4]*1e-4
    destSecX = self.gapJunctions[startRow:endRow,5]*1e-4

    # conductances are stored in pS, Neuron wants it in microsiements??!
    # (reason for not storing SI units is that we use INTs)
    conductance = self.gapJunctions[startRow:endRow,10]*1e-6

    destLoc = self.neurons[destID].mapIDtoCompartment(destSecID)
    
    for (srcID,srcSecID,srcSecX,dstLoc,dstSecX) \
        in zip(sourceID,sourceSecID,sourceSecX,
               destLoc,destSecX):

      srcLoc = self.neurons[srcID].mapIDtoCompartment(srcSecID)[0]

      self.addGapJunction(section=srcLoc,
                          sectionDist=srcSecX,
                          GIDsourceGJ=GJsrcID,
                          GIDdestGJ=GJdestID)

      self.addGapJunction(section=dstLoc,
                          sectionDist=dstSecX,
                          GIDsourceGJ=GJdestID,
                          GIDdestGJ=GJsrcID)

    assert False, "!!! CONTINUE HERE"

    
  ############################################################################
  
  # This connects one neuron, need to loop over all on the node

  # !!! DEPRICATED
  def connectNeuron(self,cellID):

    print("Use connectNeuronSynapses instead")
    
    assert False, "depricated"
    #!!! this should not iterate over rows multiple times, instead should
    #!!! keep track of where it is
    
    assert not self.isVirtualNeuron[cellID], \
      "You should not call connectNeuron with a virtual neuron: " \
      + self.neurons[cellID]["name"]
    
    
    # This goes through all the synapses on the neuron, and finds all
    # presynaptic neurons

    # All rows in synapse list that target cellID
    # sourceID, sourceComp, destID, destComp
    # We ignore sourceComp since we use axon setup and place it on that instead
    # Exclude gap junctions in this first pass synapseType != 3
    idx = np.logical_and(self.network_info["synapses"][:,1] == cellID,
                         self.network_info["synapses"][:,6] != 3)

    synapseInfo = self.network_info["synapses"][idx,:]

    # Find coordinates of the synapses in the original coordinate frame
    # by using dendOrig (old) instead of dend (which has the actual locations)

    # synapseInputLoc = self.network_info["origSynapseCoords"][idx,:]
    

    sourceCellID = synapseInfo[:,0]
    # locType = synapseInfo[:,4]
    synapseType = synapseInfo[:,6]
    axonDistance = synapseInfo[:,7]

    secID = synapseInfo[:,9]
    secX = synapseInfo[:,10]/1000.0 # Convert to number 0-1

    # This is just a double check, to make sure what I do matches
    # the ball and stick example (trying to figure out why code
    # works in serial but not parallel)
    assert self.pc.gid2cell(cellID) == self.neurons[cellID].icell, \
      "GID mismatch: " + str(self.pc.gid2cell(cellID)) \
      + " != " + str(self.neurons[cellID].icell)

    # Get the sections
    dendSection = self.neurons[cellID].mapIDtoCompartment(secID)


    
    for (srcID,section,sectionX,sType,axonDist) \
        in zip(sourceCellID,dendSection,secX,synapseType,axonDistance):

      if(sType == 3):
        # We deal with gap junctions further down
        continue


      try:

        #self.writeLog("Node: " + str(int(self.pc.id())) \
        #              + " srcID = " + str(srcID) + " loc = " + str(loc))

        #self.writeLog("Source status: " + str(int(self.pc.gid_exists(srcID))) \
        #              + " dest status: " + str(int(self.pc.gid_exists(cellID))))
          
        self.addSynapse(cellIDsource=srcID,
                        dendCompartment=section,
                        sectionDist=sectionX,
                        synapseType=self.synapseTypeLookup[sType],
                        axonDist=axonDist)
      except Exception as e:
        self.writeLog("problem in addSynapse: " + str(e))
        import pdb
        pdb.set_trace()

############################################################################

  def findGapJunctionCompartments(self):

    allLoc = dict([])
    
    origGJcoords = self.network_info["origGJCoords"]
    
    for ID in self.neuronID:

      idxGJ1 = np.where(np.logical_and(self.network_info["synapses"][:,0]==ID,\
                              self.network_info["synapses"][:,5] == 3))

      idxGJ2 = np.where(np.logical_and(self.network_info["synapses"][:,2]==ID,\
                              self.network_info["synapses"][:,5] == 3))
      
      # Coordinates on either side of the gap junction
      GJcoords1 = np.array([origGJcoords[x][0:3] for x in idxGJ1[0]])
      GJcoords2 = np.array([origGJcoords[x][3:6] for x in idxGJ2[0]])
      
      GJid1 = np.array([origGJcoords[x][6:8] for x in idxGJ1[0]])
      GJid2 = np.array([origGJcoords[x][6:8] for x in idxGJ2[0]])      
      
      if(GJcoords1.shape[0] == 0):
        GJcoords = GJcoords2
      elif(GJcoords2.shape[0] == 0):
        GJcoords = GJcoords1
      else:
        GJcoords = np.concatenate([GJcoords1,GJcoords2],axis=0)

      GJlocType = 4*np.ones(shape=(GJcoords.shape[0],1))

      lenGJ1 = len(GJcoords1)
      
      #import pdb
      #pdb.set_trace()
      
      if(GJcoords.shape[0] > 0):

        self.writeLog("Looking for " + str(GJcoords.shape[0]) +" gap junctions")
        
        # Get the compartment location of each coordinate
        GJdendLoc = self.neurons[ID].findDendCompartment(GJcoords,
                                                         GJlocType,
                                                         self.sim)
        GJdendLoc1 = GJdendLoc[:lenGJ1]
        GJdendLoc2 = GJdendLoc[lenGJ1:]

        assert(GJcoords1.shape[0] == len(GJdendLoc1))
        assert(GJcoords2.shape[0] == len(GJdendLoc2))

        for (idx,loc,id1) in zip(idxGJ1[0],GJdendLoc1,GJid1):
          allLoc[(idx,1)] = (loc,id1[0],id1[1])
      
        for (idx,loc,id2) in zip(idxGJ2[0],GJdendLoc2,GJid2):
          allLoc[(idx,2)] = (loc,id2[0],id2[1])

    return allLoc
    
    
  ############################################################################

  def addSynapse(self, cellIDsource, dendCompartment, sectionDist, conductance,
                 parameterID,synapseTypeID,axonDist=None):

    # You can not locate a point process at
    # position 0 or 1 if it needs an ion   
    if(sectionDist == 0.0):
      sectionDist = 0.01
    if(sectionDist == 1.0):
      sectionDist = 0.99

    (channelModule,parData) = self.synapseParameters[synapseTypeID]

    syn = channelModule(dendCompartment(sectionDist))

    if(parData is not None):
      # Picking one of the parameter sets stored in parData
      parID = parameterID % len(parData)

      parSet = parData[parID]
      for par in parSet:
        if(par == "expdata"):
          # Not a parameter
          continue
        
        try:
          evalStr = "syn." + par + "=" + str(parSet[par])
          self.writeLog("Updating synapse: " + evalStr)
          # !!! Can we avoid an eval here, it is soooo SLOW
          exec(evalStr)
        except:
          import traceback
          tstr = traceback.format_exc()
          print(tstr)
          import pdb
          pdb.set_trace()
        
        
    # Just create a default expsyn for test, will need to create proper GABA
    # synapses later
    #if(synapseType == 'ExpSyn'):
    #  syn = self.sim.neuron.h.ExpSyn(dendCompartment(sectionDist))
    #elif(synapseType == 'GABA'):
    #  syn = self.sim.neuron.h.tmGabaA(dendCompartment(sectionDist))
    #elif(synapseType == "AMPA_NMDA"):
    #  syn = self.sim.neuron.h.tmGlut(dendCompartment(sectionDist))
    #else:
    #  self.writeLog("Synapse type not implemented: ", synapseType)
    #  import pdb
    #  pdb.set_trace()

    if(axonDist is not None):
      # axon dist is in micrometer, want delay in ms
      synapseDelay = (1e3*1e-6*axonDist)/self.axonSpeed + self.synapseDelay
    else:
      synapseDelay = self.synapseDelay

    if(False):
      self.writeLog("Synapse delay: " + str(synapseDelay) + " ms")
      
    # What do we do if the GID does not exist?
    # print("GID exists:" + str(self.pc.gid_exists(cellIDsource)))

    if(self.isVirtualNeuron[cellIDsource]):
      # Source is a virtual neuron, need to read and connect input      
      srcName = self.network_info["neurons"][cellIDsource]["name"]
      
      # self.writeLog("Connecting " + srcName + " to " + str(dendCompartment))
      
      # !!! OLD CODE, WRONG? DEL IT
      # (v,vs,spikes) = self.virtualNeurons[cellIDsource]["spikes"]
      # nc = h.NetCon(vs,syn)

      nc = self.pc.gid_connect(cellIDsource, syn)
      nc.weight[0] = conductance
      nc.delay = synapseDelay
      nc.threshold = self.spikeThreshold

      self.netConList.append(nc)
      self.synapseList.append(syn)

      
    else:
      # print("GID connect " + str(cellIDsource) + " syn: " + str(syn))
      # print("GID exists:" + str(self.pc.gid_exists(cellIDsource)))
      
      nc = self.pc.gid_connect(cellIDsource, syn)
      nc.weight[0] = conductance
      nc.delay = synapseDelay
      nc.threshold = self.spikeThreshold
      
      self.netConList.append(nc)
      self.synapseList.append(syn)
    
    return syn

  ############################################################################

  # Add one gap junction to specific location
  
  def addGapJunction(self, \
                     section, sectionDist, \
                     GIDsourceGJ, GIDdestGJ, \
                     gGapJunction=0.5e-9, \
                     GID=None): # GID unused??

    # self.writeLog("Adding src = " + str(GIDsourceGJ) + ", dest = " + str(GIDdestGJ))
    
    GJ = h.gGapPar(section(sectionDist))
    self.gapJunctionList.append(GJ)
    
    pc.target_var(GJ._ref_vgap, GIDdestGJ)

    # !!! The line below sometimes gives this error:
    # /cfs/klemming/nobackup/h/hjorth/ChINopt/model/x86_64/special: source var gid already in use: 17124416

    pc.source_var(section(sectionDist)._ref_v, GIDsourceGJ,sec=section)
    
    GJ.g = gGapJunction

             
  ############################################################################

  def findAllLoc(self):

    # These are the location of all post-synaptic synapses targeted
    # by neurons on this node
    destLoc = []

    # These are the post-synaptic synapses which are located on this node
    # which needs to be sent to other nodes

    
    # nNeurons = len(self.network_info["neuron_info"])
    # nodeLoc =  [[] for i in range(nNeurons)]
    nodeLoc =  [[] for i in range(int(self.pc.nhost()))]
    totSynapseCtr = 0
    
    # For each neuron on the node, find where all the synapses are located
    for cellID in self.neuronID:
      # Count how many synapses added on this neuron
      synapseCtr = 0

      # All synapses targeting this neuron
      idx = (self.network_info["synapses"][:,2] == cellID)
      synapseInfo = self.network_info["synapses"][idx,:]

      # Locations of all synapses (in the original SWC coordinate frame)
      synapseInputLoc = self.network_info["origSynapseCoords"][idx,:]

      sourceCellID = synapseInfo[:,0]
      destCellID = synapseInfo[:,2]
      locType = synapseInfo[:,4]
      synapseType = synapseInfo[:,5]
    
      dendLoc = self.neurons[cellID].findDendCompartment(synapseInputLoc,
                                                         locType,
                                                         self.sim)
      
      # We need to store the synapses on the target cells so we can find them
      # later when we connect the network together

      for srcID,destID,dLoc,sType \
          in zip(sourceCellID, destCellID, dendLoc, synapseType):

        # We need to add the synapse
        synType=self.synapseTypeLookup[sType]
        dendCompartment=dLoc[0]
        sectionDist=dLoc[1]
        
        if(synType == 'ExpSyn'):
          syn = self.sim.neuron.h.ExpSyn(dendCompartment(sectionDist))
        elif(synType == 'GABA'):
          syn = self.sim.neuron.h.tmGabaA(dendCompartment(sectionDist))
        elif(synType == "AMPA_NMDA"):
          syn = self.sim.neuron.h.tmGlut(dendCompartment(sectionDist))
        else:
          self.writeLog("Synapse type not implemented: ", synapseType)
          import pdb
          pdb.set_trace()       
        
        self.synapseList.append(syn)
        self.neurons[cellID].icell.synlist.append(syn)

        synID = synapseCtr        
        synapseCtr += 1
        totSynapseCtr += 1

        # Sanity check
        try:
          assert(len(self.synapseList) == totSynapseCtr)
          assert(len(self.neurons[cellID].icell.synlist) == synapseCtr)
        except Exception as e:
          self.writeLog("Sanity check: " + str(e))
          import pdb
          pdb.set_trace()
          
        nodeID = srcID % int(self.pc.nhost())
        assert(cellID == destID)
        
        nodeLoc[nodeID].append([srcID,destID,[str(dLoc[0]),dLoc[1]],int(synID)])

    self.writeLog("nhosts = " + str(int(self.pc.nhost())))
    self.writeLog("len(nodeLoc) = " + str(len(nodeLoc)))

    # import pdb
    # pdb.set_trace()

    self.writeLog("About to transfer data")
    # self.writeLog("nodeLoc = " + str(nodeLoc))

    # Transfer all the data between nodes
    
    self.pc.barrier()
    data = self.pc.py_alltoall(nodeLoc)
    self.pc.barrier()
    self.writeLog("All data transferred")
    
    # This is a list of lists, one element per node in the simulation
    # each element is a list with synapses, srcID, destID, dendLoc,synapseID
    return data 
  
  ############################################################################
  def addCurrentInjection(self, amp, cellID=None):
    
    if(cellID is None):
      cellID = self.neuronID
    
    cells = dict((k,self.neurons[k]) \
                 for k in cellID if not self.isVirtualNeuron[k])
    
    for cell in cells.values():
      
      Istim           =   self.sim.neuron.h.IClamp(0.5, sec=cell.icell.soma[0]) # get sec
      Istim.delay     =   100
      Istim.dur       =   3000
      Istim.amp       =   amp*1e-3
      self.istim.append(Istim)
  
  ############################################################################
  
  
  def addCurrentFromProtocol(self):
    
    # load config
    with open(self.config_file,'r') as cf:
      config_file = json.load(cf)
      
    # find name 
    for neuronID, neuron in self.neurons.items():
      name = neuron.name
      
      # get protocol
      prot_file_name = config_file[name]["protocols"]
      with open(prot_file_name,'r') as pfile:
        protocols = json.load(pfile)
        
      # get one of the superthreshold protocols
      count= 0
      for key,pd in list(protocols.items()):
        if 'sub' in key or 'IV' in key: continue
        print( '\t', count, key, pd['stimuli'][0]['amp'], name, config_file[name]["protocols"] )
        break
        
      # baseline injection
      Istim           =   self.sim.neuron.h.IClamp(0.5, \
                            sec=self.neurons[neuronID].icell.soma[0])
      Istim.delay     =   0
      Istim.dur       =   11000
      Istim.amp       =   pd['stimuli'][1]['amp']
      self.istim.append(Istim)
      # driver injection
      Istim           =   self.sim.neuron.h.IClamp(0.5, \
                            sec=self.neurons[neuronID].icell.soma[0])
      Istim.delay     =   200   #+neuronID*1000
      Istim.dur       =   1500
      Istim.amp       =   pd['stimuli'][0]['amp']
      self.istim.append(Istim)
  
  
      
  ############################################################################
  def setDopamineModulation(self,sec,transient):
    channel_sufix = ['naf_ms', 'kas_ms', 'kaf_ms', 'kir_ms', 'cal12_ms', 'cal13_ms', 'can_ms', 'car_ms']
    for seg in sec:
      for mech in seg:
        if mech.name() in channel_sufix:
          mech.damod = 1
          if not transient: mech.level = 1
          else:
            transient.play(mech._ref_level, self.sim.neuron.h.dt)
            self.modTrans.append( transient )
              
  
  def applyDopamine(self, cellID=None, play=0):
    ''' activate dopamine modulation
    play argument decides how the modulation is implemented:
        0 - bath apply (instant full modulation)
        else - plays into level variable of mech.
    '''
    if play: transient = self.sim.neuron.h.Vector(play)
    else   : transient = play
    
    if(cellID is None):
      cellID = self.neuronID
    
    cells = dict((k,self.neurons[k]) \
                 for k in cellID if not self.isVirtualNeuron[k])
    for cell in cells.values():
      #print(cell.icell)
      for sec in cell.icell.dend:
        self.setDopamineModulation(sec, transient)
      for sec in cell.icell.axon:
        self.setDopamineModulation(sec, transient)
      for sec in cell.icell.soma: # get sec
        self.setDopamineModulation(sec, transient)
        
                    
        
  ############################################################################

  # Wilson 2007 - GABAergic inhibition in the neostriatum
  # 80% of synapses in Striatum are glutamatergic
  # Estimated 10000 glutamate and 2000 GABA synapses per MS,
  # 325 dopamine synapses per MS
  # Wilson 1996 - 10000 spines per MS = 10000 glutamatergic inputs

  # Ingham et al 1998, approx 1 glutamatergic synapse per 0.92 mum3
  # --> ~11000 glutamate synapses per MS
  # Kemp 1971 -- The synaptic organization of the caudate nucleus (85% glu)


  def addExternalInput(self,inputFile=None):

    if(inputFile is None):
      inputFile = self.inputFile

    
    self.writeLog("Adding external (cortical, thalamic) input from " \
                  + inputFile)

    self.inputData = h5py.File(inputFile,'r')

    for neuronID, neuron in self.neurons.items():

      # !!! WE ALSO NEED TO HANDLE modFile and parameterFile parameters that are
      # in inputData 
      
      self.externalStim[neuronID] = []
      name = neuron.name
      
      if(str(neuronID) not in self.inputData["input"]):
        self.writeLog("Warning - No input specified for " + name)
        continue
      
      for inputType in self.inputData["input"][str(neuronID)]:
        neuronInput = self.inputData["input"][str(neuronID)][inputType]
        
        locType = 1*np.ones((neuronInput["sectionID"].shape[0],)) # Axon-Dend

        sections = self.neurons[neuronID].mapIDtoCompartment(neuronInput["sectionID"])

        # Setting individual parameters for synapses
        modFile = neuronInput["modFile"].value
        paramList = json.loads(neuronInput["parameterList"].value)

        evalStr = "self.sim.neuron.h." + modFile
        channelModule = eval(evalStr)
        
        for inputID,section,sectionX,paramID in zip(neuronInput["spikes"],
                                                    sections,
                                                    neuronInput["sectionX"],
                                                    neuronInput["parameterID"]):
          # We need to find cellID (int) from neuronID (string, eg. MSD1_3)
          
          idx = int(inputID)
          spikes = neuronInput["spikes"][inputID].value * 1e3 # Neuron uses ms

          if(False):
            print("Neuron " + str(neuron.name) + " receive " + str(len(spikes)) + " spikes from " + str(inputID))
            if(len(spikes) > 0):
              print("First spike at " + str(spikes[0]) + " ms")
          
          # Creating NEURON VecStim and vector
          # https://www.neuron.yale.edu/phpBB/viewtopic.php?t=3125
          #import pdb
          #pdb.set_trace()
          try:
            vs = h.VecStim()
            v = h.Vector(spikes.size)
            v.from_python(spikes)
            vs.play(v)
          except:
            print("!!! If you see this, make sure that vecevent.mod is included in nrnivmodl compilation")
            
            import traceback
            tstr = traceback.format_exc()
            print(tstr)
            import pdb
            pdb.set_trace()
 

          # NEURON: You can not locate a point process at position 0 or 1
          # if it needs an ion
          if(sectionX == 0.0):
            sectionX = 0.01
          elif(sectionX == 1.0):
            sectionX = 0.99

          # !!! Parameters for the tmGlut should be possible to set in the
          # input specification !!!
          #syn = self.sim.neuron.h.tmGlut(section(sectionX))
          syn = channelModule(section(sectionX))
          nc = h.NetCon(vs,syn)
          
          nc.delay = 0.0
          # Should weight be between 0 and 1, or in microsiemens?
          nc.weight[0] = neuronInput["conductance"].value * 1e6 # !! what is unit? microsiemens?
          nc.threshold = 0.1

          if(False):
            print("Weight: " + str(nc.weight[0]))

          # Get the modifications of synapse parameters, specific to
          # this synapse
          if(paramList is not None and len(paramList) > 0):
            synParams = paramList[paramID % len(paramList)]

            for par in synParams:
              if(par == "expdata"):
                # Not a parameter
                continue

              try:

                evalStr = "syn." + par + "=" + str(synParams[par])
                self.writeLog("Updating synapse: " + evalStr)
                # !!! Can we avoid an eval here, it is soooo SLOW
                exec(evalStr)

              except:
                import traceback
                tstr = traceback.format_exc()
                print(tstr)
                import pdb
                pdb.set_trace()
                 
                
              

          # !!! Set parameters in synParams
          
          # Need to save references, otherwise they will be freed
          # So sorry, but that is how neuron is
          self.externalStim[neuronID].append((v,vs,nc,syn,spikes))
                    
          # ps = h.PatternStim()

          # HOW DO WE USE PATTERNSTIM?
          

  ############################################################################

  def addVirtualNeuronInput(self):

    self.writeLog("Adding inputs from virtual neurons")

    
          
  ############################################################################
  
  # This adds external input (presumably cortical and thalamic)
  def addExternalInputOLD(self,nInputs=50,freq=5.0):
    self.writeLog("Adding external (cortical, thalamic) input (OLD VERSION)")
    assert False, "This uses neuron generated spikes, please use new function"
    
    for neuronID in self.neurons:
      neuron = self.neurons[neuronID]
      
      self.externalStim[neuronID] = []
      
      for i in range(0,nInputs):
        #if(neuronID != 1): # !!! TEST, only input to one neuron to see synapses work
        #  continue
        try:
          randComp = np.random.choice(neuron.icell.dend)
          randDist = np.random.random(1)
          
          netStim = self.sim.neuron.h.NetStim()
          netStim.start = 0
          netStim.interval = 1000.0/freq # units ms :(
          
          # self.writeLog("Interval " + str(netStim.interval) + " ms")
            
          netStim.noise = 1.0
          netStim.number = 10000
          
          self.externalStim[neuronID].append(netStim)

          # self.writeLog("RandComp: " + str(randComp) \
          #               + "dist: " + str(randDist[0]))
            
          syn = self.sim.neuron.h.tmGlut(randComp(randDist[0]))
          nc = self.sim.neuron.h.NetCon(netStim,syn)
          nc.delay = 1
          nc.weight[0] = 0.1
          nc.threshold = 0.1
          
          self.netConList.append(nc)
          self.synapseList.append(syn)
        except Exception as e:
          self.writeLog("Error! " + str(e))
          import pdb
          pdb.set_trace()

  ############################################################################

  # This code uses PatternStim to send input to neurons from file

  # inputFile is a csv file
  # neuronID, synapseLocation, spikeFile
  
  
#  def addExternalInput(self, inputFile=None):
#
#    if(inputFile is None):
#      inputFile = self.inputFile
#    
#    for neuronID, neuron in self.neurons.items():
#      # We must store tvect and idvect, to avoid them being garbage collected
#      # same with PatternStim
#
#      # How should we specify where the input is stored, and where
#      # on the neuron it should be placed?
#      for inputFiles in neuron.input:
#      
#        ps = h.PatternStim()
#
#        #!!! CODE NOT DONE, FIGURE OUT
#        assert(False)
          
  ############################################################################

  def centreNeurons(self,sideLen=None,neuronID=None):
    if(neuronID is None):
      neuronID = self.neuronID

    if(sideLen is None):
      return neuronID

    cID = []

    positions = self.network_info["neuronPositions"]

    centrePos = np.min(positions,axis=0)
    
    for nid in neuronID:
      # pos = self.network_info["neurons"][nid]["position"]
      pos = positions[nid,:]
      
      if(abs(pos[0]-centrePos[0]) <= sideLen
         and abs(pos[1]-centrePos[1]) <= sideLen
         and abs(pos[2]-centrePos[2]) <= sideLen):
        cID.append(nid)

    print("Centering: Keeping " + str(len(cID)) + "/" + str(len(neuronID)))
        
    return cID
    
  
  ############################################################################
  
  def addRecording(self,cellID=None,sideLen=None):
    self.writeLog("Adding somatic recordings")
    
    if(cellID is None):
      cellID = self.neuronID

    cellID = self.centreNeurons(sideLen=sideLen,neuronID=cellID)

    cells = dict((k,self.neurons[k]) \
                 for k in cellID if not self.isVirtualNeuron[k])
    
    
    if(len(self.tSave) == 0 or self.tSave is None):
      self.tSave = self.sim.neuron.h.Vector()
      self.tSave.record(self.sim.neuron.h._ref_t)
      
    for cellKey in cells:
      cell = cells[cellKey]
      try:
        v = self.sim.neuron.h.Vector()
        #import pdb
        #pdb.set_trace()
        v.record(getattr(cell.icell.soma[0](0.5),'_ref_v'))
        self.vSave.append(v)
        self.vKey.append(cellKey)
      except Exception as e:
        self.writeLog("Error: " + str(e))
        import pdb
        pdb.set_trace()
        
  ############################################################################
        
  def run(self,t=1000.0):

    # If we want to use a non-default initialisation voltage, we need to 
    # explicitly set: h.v_init 
    
    # Make sure all processes are synchronised
    self.pc.barrier()
    self.writeLog("Running simulation for " + str(t/1000) + " s")
    # self.sim.psolve(t)
    self.sim.run(t,dt = 0.025)
    self.pc.barrier()
    self.writeLog("Simulation done.")
    
    
  ############################################################################
    
  def plot(self):    
    import matplotlib.pyplot as pyplot
    pyplot.figure()
    for v in self.vSave:
      pyplot.plot(self.tSave,v)
      
    pyplot.xlabel('Time (ms)')
    pyplot.ylabel('Voltage (mV)')
    pyplot.show()
    from os.path import basename
    name = basename(self.network_info["configFile"])
    pyplot.savefig('figures/Network-voltage-trace' + name + '.pdf')

  ############################################################################

  def getSpikes(self):
    spiketrain.netconvecs_to_listoflists(self.tSpikes,self.idSpikes)

  ############################################################################

  def writeSpikes(self,outputFile='save/network-spikes.txt'):
    for i in range(int(self.pc.nhost())):
      self.pc.barrier() # sync all processes
      if(i == int(self.pc.id())):
        if(i == 0):
          mode = 'w'
        else:
          mode = 'a'
        with open(outputFile,mode) as spikeFile:
          for (t,id) in zip(self.tSpikes,self.idSpikes):
            spikeFile.write('%.3f\t%d\n' %(t,id))
      self.pc.barrier()

  ############################################################################

# File format for csv voltage file:
# -1,t0,t1,t2,t3 ... (time)
# cellID,v0,v1,v2,v3, ... (voltage for cell #ID)
# repeat
  
  def writeVoltage(self,outputFile="save/traces/network-voltage", cellID=None):
    
    for neuronID, neuron in self.neurons.items():
        name = neuron.name
    
    for i in range(int(self.pc.nhost())):
      self.pc.barrier()
      
      if(i == int(self.pc.id())):
        if(i == 0):
          mode = 'w'
        else:
          mode = 'a'
         
        with open(outputFile,mode) as voltageFile:
          if(mode == 'w'):
            voltageFile.write('-1') # Indiciate that first column is time
            
            #for t in self.tSave:
            #  voltageFile.write(',%.4f' % t)
            
          for vID, voltage in zip(self.vKey,self.vSave):
            #self.neurons[i]['
            voltageFile.write('\n{}'.format(name))
            for v in voltage:
              voltageFile.write(',%.4f' % v)
         
      self.pc.barrier()
    
##############################################################################

  def writeLog(self,text):
    if(self.logFile is not None):
      self.logFile.write(text + "\n")
      print(text)
    else:
      if(self.verbose):
        print(text)

############################################################################

  def createDir(self,dirName):
    if(not os.path.isdir(dirName)):
      print("Creating " + str(dirName))
      os.makedirs(dirName)

        
############################################################################

def findLatestFile(fileMask):

  files = glob(fileMask)
  
  modTime = [os.path.getmtime(f) for f in files]
  idx = np.argsort(modTime)

  return files[idx[-1]]

############################################################################

def alpha(ht, tstart, tau):
    ''' 
    calc and returns a "magnitude" using an alpha function -> used for [DA].
    
    ht      = simulation time (h.t)
    tstart  = time when triggering the function
    tau     = time constant of alpha function
    '''
    
    t   = (ht - tstart) / tau
    e   = np.exp(1-t)
    mag = t * e
    
    if mag < 0: mag = 0
    return mag

############################################################################

#
# Test code to run a simulation

if __name__ == "__main__":
  # First run Network_connect_TEST.sh to generate network connectivity
  # print("This code assumes Network_connect_TEST.sh has created hdf5 file with network connectivity")

  import timeit
  start = timeit.default_timer()
  
  import sys
  
  SlurmID = os.getenv('SLURM_JOBID')
  
  pc = h.ParallelContext()
  
  if('-python' in sys.argv):
    pythonidx = sys.argv.index('-python')
    if(len(sys.argv) > pythonidx):
      argstr = sys.argv[pythonidx+1:]
    else:
      argstr = []
  else:
    argstr = sys.argv
    
  if(len(argstr) > 1):
    networkDataFile = argstr[1]
    if(networkDataFile == 'last'):
       networkDataFile = findLatestFile('save/network-connect-voxel-pruned-synapse-file-*.hdf5')
  else:
    networkDataFile = findLatestFile('save/network-connect-voxel-pruned-synapse-file-*.hdf5')
  if(len(argstr) > 2):
    inputFile = argstr[2]
    if(inputFile == 'last'):
      inputFile = findLatestFile('save/input-spikes-*hdf5')
  else:
    inputFile = findLatestFile('save/input-spikes-*hdf5')
  if(SlurmID is None):
    digits = re.findall(r'\d+', inputFile)
    # Second to last digit is slurmID of old run, reuse that
    try:
      SlurmID = digits[-2]
    except:
      print("Failed to auto detect SlurmID, defaulting to 666")
      SlurmID = str(666)
  disableGJ = True # !!!! FOR NOW
  if(disableGJ):
    print("!!! WE HAVE DISABLED GAP JUNCTIONS !!!")
  
  
  # ======================================================================
  
  tSim = 1500        # ms
  #iamp = 256        # pA
  disableSynapses = 0
  
  # ======================================================================
  
  
  sim = NetworkSimulate(network_info_file=networkDataFile,
                        inputFile=inputFile,
                        disableGapJunctions=disableGJ,
                        disableSynapses=disableSynapses)
  sim.addRecording(sideLen=None)  
  #sim.addCurrentInjection( iamp )
  sim.addCurrentFromProtocol()
  
  v = [alpha(ht, 500, 500) for ht in np.arange(0,1500,0.025)]
  
  for r,tag in enumerate(['ctrl','da','trans']):
    if tag == 'trans':
      sim.applyDopamine(play=v)
    elif tag == 'da':
      sim.applyDopamine() 
    print(tag)
    sim.run(tSim)
    sim.writeSpikes('save/traces/network-output-spikes-{}_orgM_disSyn{}.txt'.format(tag, disableSynapses))
    sim.writeVoltage('save/traces/network-voltage-{}_orgM_disSyn{}.csv'.format(tag, disableSynapses))
   
  exit(0)

# Check this code example
# Why are spikes not propagated from one neuron to another
# https://senselab.med.yale.edu/modeldb/ShowModel.cshtml?model=188544&file=%2FLyttonEtAl2016%2FREADME.html#tabs-2
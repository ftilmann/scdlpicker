#!/usr/bin/env python3
# -*- coding: utf-8 -*-
###########################################################################
# Copyright (C) GFZ Potsdam                                               #
# All rights reserved.                                                    #
#                                                                         #
# Author: Joachim Saul (saul@gfz-potsdam.de)                              #
#                                                                         #
# GNU Affero General Public License Usage                                 #
# This file may be used under the terms of the GNU Affero                 #
# Public License version 3.0 as published by the Free Software Foundation #
# and appearing in the file LICENSE included in the packaging of this     #
# file. Please review the following information to ensure the GNU Affero  #
# Public License version 3.0 requirements will be met:                    #
# https://www.gnu.org/licenses/agpl-3.0.html.                             #
###########################################################################

import sys
import time
import pathlib
import numpy
import seiscomp.core
import seiscomp.client
import seiscomp.datamodel
import seiscomp.logging
import seiscomp.math
import seiscomp.seismology
import scdlpicker.inventory as _inventory
import scdlpicker.util as _util
import scdlpicker.dbutil as _dbutil
import scdlpicker.eventworkspace as _ews
import scdlpicker.config as _config

# Below are parameters that for the time being are hardcoded.

# That's me! This author ID will be written into all new picks.
author = "dlpicker"

# The acquisition will wait that long to finalize the acquisition
# of waveform time windows. The processing may be interrupted that
# long!
streamTimeout = 5

# Send new picks to this group.
targetMessagingGroup = "MLTEST"

# Ignore objects (picks, origins) from these authors
ignoredAuthors = []

# We may receive origins from other agencies, but don't want to
# process them. Add the agency ID's here. Any origin with agencyID
# in this list will be ignored.
ignoredAgencyIDs = []

# We normally never process empty origins. However, we may receive
# origins from other agencies that come without arrivals. We usually
# consider these trustworthy agencies and for such origins we do
# want to search for matching, previously missed picks.
emptyOriginAgencyIDs = []

# DON'T change this
timeoutInterval = 1


def alreadyRepicked(pick):
    """
    This is not a repick but a repick for this exits already

    TODO: check if a repick of this pick exists or has been
    attempted
    """
    pass


def isRepick(pick, author=None):
    """
    Is this already a repick?
    """

    methodIDs = ["DL", "PhaseNet", "PHN", "EQTransformer", "EQT", "XYZ"]
    try:
        if pick.methodID() in methodIDs:
            return True
    except (AttributeError, ValueError):
        pass

    try:
        if author is not None and pick.creationInfo().author() == author:
            return True
    except (AttributeError, ValueError):
        pass

    return False


class App(seiscomp.client.Application):

    def __init__(self, argc, argv):
        super().__init__(argc, argv)
        self.ignoredAuthors = ignoredAuthors
        self.ignoredAgencyIDs = ignoredAgencyIDs
        self.emptyOriginAgencyIDs = emptyOriginAgencyIDs

        self.setDatabaseEnabled(True, True)
        self.setLoadInventoryEnabled(True)

        # We need the config to determine which streams are used for picking
        self.setLoadConfigModuleEnabled(True)

        # We want to stream waveform data and save the raw records
        self.setRecordStreamEnabled(True)
        # self.setRecordInputHint(seiscomp.core.Record.SAVE_RAW)

        self.workspaces = dict()

        # Keep track of events that need to be processed. We process
        # one event at a time. In this dict we register the events
        # that require processing but we delay processing until
        # previous events are finished.
        self.pendingEvents = dict()

        self.ttt = None

    def initConfiguration(self):
        # Called before validateParameters()

        if not super().initConfiguration():
            return False

        try:
            self.targetMessagingGroup = \
                self.configGetString("scdlpicker.messagingGroup")
        except RuntimeError:
            self.targetMessagingGroup = targetMessagingGroup

        try:
            self.ignoredAuthors = \
                self.configGetStrings("scdlpicker.ignoredAuthors")
        except RuntimeError:
            self.ignoredAuthors = ignoredAuthors
        self.ignoredAuthors = list(self.ignoredAuthors)
        self.ignoredAuthors.append(self.author())

        try:
            self.ignoredAgencyIDs = \
                self.configGetStrings("scdlpicker.ignoredAgencyIDs")
        except RuntimeError:
            self.ignoredAgencyIDs = []
        self.ignoredAgencyIDs = list(self.ignoredAgencyIDs)

        try:
            emptyOriginAgencyIDs = \
                self.configGetStrings("scdlpicker.emptyOriginAgencyIDs")
        except RuntimeError:
            pass
        self.emptyOriginAgencyIDs = list(emptyOriginAgencyIDs)

        try:
            self.streamTimeout = self.configGetDouble("scdlpicker.streamTimeout")
        except RuntimeError:
            self.streamTimeout = streamTimeout

        return True

    def dumpConfiguration(self):
        info = seiscomp.logging.info

        info("Global parameters")
        info("  agency = " + self.agencyID())
        info("  author = " + self.author())
        self.commonConfig.dump(info)
        self.pickingConfig.dump(info)
        info("Local parameters")
        info("  messagingGroup = " + str(self.targetMessagingGroup))
        info("  ignoredAuthors = " + str(self.ignoredAuthors))
        info("  ignoredAgencyIDs = " + str(self.ignoredAgencyIDs))
        info("  emptyOriginAgencyIDs = " + str(self.emptyOriginAgencyIDs))
        info("  streamTimeout = " + str(self.streamTimeout))

    def createCommandLineDescription(self):
        self.commandline().addGroup("Config")
        self.commandline().addStringOption(
            "Config", "working-dir,d", "Path of the working directory where intermediate files are placed and exchanged")
        self.commandline().addStringOption(
            "Config", "messaging-group,g", "messaging group to send picking results to")
        self.commandline().addStringOption(
            "Config", "ignored-authors", "comma-separated list of data authors to ignore")

        self.commandline().addGroup("Test")
        self.commandline().addStringOption("Test", "event,E", "ID of event to test")

        return True

    def validateParameters(self):
        """
        Command-line parameters
        """
        if not super().validateParameters():
            return False

        if self.commandline().hasOption("messaging-group"):
            self.targetMessagingGroup = self.commandline().optionString("messaging-group")

        self.setMessagingEnabled(False)
        if not self.commandline().hasOption("event"):
            # not in event mode -> configure the messaging
            self.setMessagingEnabled(True)
            self.setPrimaryMessagingGroup(self.targetMessagingGroup)
            self.addMessagingSubscription("PICK")
            self.addMessagingSubscription("LOCATION")
            self.addMessagingSubscription("EVENT")

        return True

    def init(self):
        if not super().init():
            return False

        self.commonConfig = _config.getCommonConfig(self)

        self.workingDir = self.commonConfig.workingDir

        # This is the directory where all the event data are written to.
        self.eventRootDir = self.commonConfig.workingDir / "events"

        # This is the directory in which we create symlinks pointing to
        # data we need to work on.
        self.spoolDir = self.commonConfig.workingDir / "spool"

        # This is the directory in which results are placed by the repicker
        self.outgoingDir = self.commonConfig.workingDir / "outgoing"

        # After sending the data to the messaging, the file is move to here.
        self.sentDir = self.commonConfig.workingDir / "sent"

        self.pickingConfig = _config.getPickingConfig(self)
        self.relocationConfig = _config.getRelocationConfig(self)

        self.inventory = seiscomp.client.Inventory.Instance().inventory()

        self.setupFolders()
        now = seiscomp.core.Time.GMT()
        self.components = _inventory.streamComponents(
            self.inventory, now,
            net_sta_blacklist=self.commonConfig.stationBlacklist)

        configModule = self.configModule()
        myName = self.name()
        self.configuredStreams = \
            _util.configuredStreams(configModule, myName)

        return True

    def sendRepickerResults(self, picks, comments):
        """
        Send the repicker results contained in one YAML file.

        The YAML file is assumed to be non empty.
        """

        connection = self.connection()

        pickIDs = sorted(picks.keys())
        for pickID in pickIDs:
            # Set creation time for each pick with the goal of preventing
            # exactly identical creation times.
            time.sleep(0.0001)  # wait for 100 microseconds
            now = seiscomp.core.Time.GMT()
            ctime = now
            ci = _util.creationInfo(author, self.agencyID(), ctime)
            pick = picks[pickID]
            pick.setCreationInfo(ci)

        ep = seiscomp.datamodel.EventParameters()
        seiscomp.datamodel.Notifier.Enable()
        for pickID in pickIDs:
            pick = picks[pickID]
            ep.add(pick)
            if pickID in comments:
                for comment in comments[pickID]:
                    pick.add(comment)
        msg = seiscomp.datamodel.Notifier.GetMessage()
        seiscomp.datamodel.Notifier.Disable()
        if connection.send(msg):
            for pickID in pickIDs:
                seiscomp.logging.info("sent " + pickID)
            seiscomp.logging.info("sent %d picks" % (len(picks),))
            return True
        else:
            for pickID in picks:
                seiscomp.logging.info("failed to send " + pickID)
            return False

    def handleTimeout(self):
        # The timeout interval can be configured via timeoutInterval
        self.pollRepickerResults()
        self.processPendingEvents()

    def pollRepickerResults(self):
        # Poll for repicker results between each event.
        #
        # Note that we poll here for any results, not just the current
        # event.
        # This doesn't cost much and in case of aftershocks it is really
        # needed in order to avoid long delays or deadlocks.
        repickerResults = _util.pollRepickerResults(self.outgoingDir)
        if repickerResults:
            for yamlfile in repickerResults:
                picks, comments = _util.readRepickerResults(yamlfile)
                if self.sendRepickerResults(picks, comments):
                    sent = self.sentDir / yamlfile.name
                    yamlfile.rename(sent)

    def processPendingEvents(self):
        for eventID in sorted(self.pendingEvents.keys()):
            event = self.pendingEvents.pop(eventID)
            self.processEvent(event)
            self.pollRepickerResults()

    def computeTravelTimes(self, delta, depth):
        if self.ttt is None:
            self.ttt = seiscomp.seismology.TravelTimeTable()
            self.ttt.setModel(self.commonConfig.earthModel)

        arrivals = self.ttt.compute(0, 0, depth, 0, delta, 0, 0)
        return arrivals

    def findUnpickedStations(self, origin, maxDelta, picks):
        """
        Find stations within maxDelta from origin which are not
        represented by any of the specified picks.
        """
        seiscomp.logging.debug(
            "findUnpickedStations for maxDelta=%g" % maxDelta)
        elat = origin.latitude().value()
        elon = origin.longitude().value()

        net_sta_blacklist = []
        for pickID in picks:
            pick = picks[pickID]
            net, sta, loc, cha = _util.nslc(pick)
            net_sta_blacklist.append((net, sta))

        predictedPicks = {}

        now = seiscomp.core.Time.GMT()
        inv = seiscomp.client.Inventory.Instance().inventory()
        inv = _inventory.InventoryIterator(inv, now)
        for network, station, location, stream in inv:
            net =  network.code()
            sta =  station.code()
            loc = location.code()
            cha =   stream.code()
            cha = cha[:2]
            if (net, sta) in net_sta_blacklist:
                continue
            if (net, sta, "--" if loc == "" else loc, cha) \
                    not in self.configuredStreams:
                continue
            slat = station.latitude()
            slon = station.longitude()
            delta, a, b = seiscomp.math.delazi(elat, elon, slat, slon)
            if delta > maxDelta:
                continue

            arrivals = self.computeTravelTimes(delta, origin.depth().value())
            firstArrival = arrivals[0]
            time = origin.time().value() + \
                seiscomp.core.TimeSpan(firstArrival.time)
            timestamp = time.toString("%Y%m%d.%H%M%S.%f000000")[:18]
            pickID = timestamp + "-PRE-%s.%s.%s.%s" % (net, sta, loc, cha)
            if seiscomp.datamodel.Pick.Find(pickID):
                continue
            predictedPick = seiscomp.datamodel.Pick(pickID)
            phase = seiscomp.datamodel.Phase()
            phase.setCode("P")
            predictedPick.setPhaseHint(phase)
            q = seiscomp.datamodel.TimeQuantity(time)
            predictedPick.setTime(q)
            wfid = seiscomp.datamodel.WaveformStreamID()
            wfid.setNetworkCode(net)
            wfid.setStationCode(sta)
            wfid.setLocationCode(loc)
            wfid.setChannelCode(cha+"Z")
            predictedPick.setWaveformID(wfid)
            # We do not set the creation info here.
            if pickID not in predictedPicks:
                predictedPicks[pickID] = predictedPick
        return predictedPicks

    def setupFolders(self):
        # if necessary, create some needed folders at startup
        for d in [self.eventRootDir, self.spoolDir,
                  self.outgoingDir, self.sentDir]:
            d.mkdir(parents=True, exist_ok=True)

    def _loadEvent(self, publicID):
        return _dbutil.loadEvent(self.query(), publicID, full=True)

    def _loadOrigin(self, publicID):
        return _dbutil.loadOrigin(self.query(), publicID, full=False)

    def _loadWaveformsForPicks(self, picks, event):
        request = dict()
        for pickID in picks:
            pick = picks[pickID]
            wfid = pick.waveformID()
            net = wfid.networkCode()
            sta = wfid.stationCode()
            loc = wfid.locationCode()
            if loc == "":
                loc = "--"
            cha = wfid.channelCode()

            # Sometimes manual picks produced by scolv have only "BH" etc. as
            # channel code. For the request that doesn't make a difference.
            if len(cha) == 2:
                cha = cha + "Z"

            # avoid requesting data that we have saved already
            eventID = event.publicID()
            key = "%s.%s.%s.%s" % (net, sta, loc, cha)
            mseedFileName = self.eventRootDir / eventID / (key + ".mseed")
            if mseedFileName.exists():
                continue

            t0 = pick.time().value()
            t1 = t0 + seiscomp.core.TimeSpan(-self.pickingConfig.beforeP)
            t2 = t0 + seiscomp.core.TimeSpan(+self.pickingConfig.afterP)
            if (net, sta, loc, cha[:2]) not in self.components:
                # This may occur if a station was (1) blacklisted or (2) added
                # to the processing later on. Either way we skip this pick.
                continue
            nslc = (net, sta, loc, cha)
            request[nslc] = (t1, t2)
            t1 = _util.isotimestamp(t1)
            t2 = _util.isotimestamp(t2)
            seiscomp.logging.debug("REQUEST %-2s %-5s %-2s %-2s %s %s"
                                   % (net, sta, loc, cha, t1, t2))

        waveforms = dict()

        if request:
            # request waveforms and dump them to one file per stream
            seiscomp.logging.info("Opening RecordStream "+self.recordStreamURL())
            stream = seiscomp.io.RecordStream.Open(self.recordStreamURL())
            stream.setTimeout(int(self.streamTimeout))
            streamCount = 0
            for nslc in sorted(request.keys()):
                net, sta, loc, cha = nslc
                t1, t2 = request[nslc]
                for comp in self.components[(net, sta, loc, cha[:2])]:
                    _loc = "" if loc == "--" else loc
                    stream.addStream(net, sta, _loc, cha[:2]+comp, t1, t2)
                    streamCount += 1

            seiscomp.logging.info(
                "RecordStream: requesting %d streams" % streamCount)
            count = 0
            for rec in _util.RecordIterator(stream, showprogress=True):
                if rec is None:
                    break
                streamID = rec.streamID()
                if streamID not in waveforms:
                    waveforms[streamID] = []
                waveforms[streamID].append(rec)
                count += 1
            seiscomp.logging.debug(
                "RecordStream: received  %d records" % (count,))

            count = 0
            for key in waveforms:
                count += len(waveforms[key])
            seiscomp.logging.debug(
                "RecordStream: received  %d records for %d streams"
                % (count, len(waveforms.keys())))

            # remove gappy streams
            seiscomp.logging.debug("Looking for gappy streams")
            gappyStreams = []
            for streamID in waveforms:
                waveforms[streamID] = _util.prepare(waveforms[streamID])
                if _util.gappy(waveforms[streamID], tolerance=1.):
                    gappyStreams.append(streamID)
            for streamID in gappyStreams:
                seiscomp.logging.warning("Gappy stream "+streamID+" ignored")

        return waveforms

    def testEvent(self, eventID,
                  skipManualOrigins=False,
                  preferredOriginOnly=False):
        """
        Test the module for the event with the specified ID.

        Real-time is simulated by loading all origins and then
        iterate over these origins in the order of their creation.
        Then for each origin we load the data from the waveform
        server but only for the picks not yet loaded.
        """
        # load event and preferred origin
        event = self._loadEvent(eventID)
        if not event:
            seiscomp.logging.error("Failed to load event "+eventID)
            return False

        seiscomp.logging.debug("Loaded event "+eventID)

        workspace = self.workspaces[eventID] = _ews.EventWorkspace()
        workspace.event = event
        workspace.origin = None
        workspace.all_picks = dict()

        origins = list()

        if preferredOriginOnly is True:
            origin = self.query().loadObject(
                seiscomp.datamodel.Origin.TypeInfo(),
                event.preferredOriginID())
            origin = seiscomp.datamodel.Origin.Cast(origin)
            origins.append(origin)
        else:
            tmp_origins = list()
            for origin in self.query().getOrigins(eventID):
                try:
                    # hack to acquire ownership
                    origin = seiscomp.datamodel.Origin.Cast(origin)
                    assert origin is not None
                    origin.creationInfo().creationTime()
                except ValueError:
                    continue
                except AssertionError:
                    continue

                if _util.manual(origin) and skipManualOrigins:
                    seiscomp.logging.debug(
                        "Skipping manual origin " + origin.publicID())
                    continue

                tmp_origins.append(origin)

            # two origin loops to prevent nested DB calls
            for origin in tmp_origins:

                # This loads everything including the arrivals
                # but is very slow
                # origin = self.query().loadObject(
                #     Origin.TypeInfo(), origin.publicID())
                # origin = Origin.Cast(origin)
                # if not origin: continue

                # See if the origin has arrivals. Of not, try to load
                # arrivals from database. If still no arrivals, give up.
                # loadArrivals() is much faster than the more
                # comprehensive loadObject()
                if origin.arrivalCount() == 0:
                    self.query().loadArrivals(origin)
                if origin.arrivalCount() == 0:
                    continue

                origins.append(origin)

        seiscomp.logging.debug("Loaded %d origin(s)" % len(origins))

        sorted_origins = sorted(
            origins, key=lambda origin: origin.creationInfo().creationTime())

        for origin in sorted_origins:
            if self.isExitRequested():
                # e.g. Ctrl-C
                return True

            self.processOrigin(origin, event)
            workspace.dump(self.eventRootDir)

        return True

    def addObject(self, parentID, obj):
        # called by the Application class if a new object is received
        event = seiscomp.datamodel.Event.Cast(obj)
        if _util.valid(event):
            self.pendingEvents[event.publicID()] = event

    def updateObject(self, parentID, obj):
        # called by the Application class if an updated object is received
        self.addObject(parentID, obj)

    def cleanup(self, timeout=30*3600):
        now = seiscomp.core.Time.GMT()
        tmin = now-seiscomp.core.TimeSpan(timeout)
        blacklist = []
        for eventID in self.workspaces:
            workspace = self.workspaces[eventID]
            if workspace.origin.time().value() < tmin:
                blacklist.append(eventID)
        for eventID in blacklist:
            del self.workspaces[eventID]

    def getAssociatedPicksForOrigin(self, originID):

        # Query all associated picks for this origin
        associated_picks = []
        objects = self.query().getPicks(originID)
        if not objects:
            seiscomp.logging.debug("no results from getPicks")
        for obj in objects:
            pick = seiscomp.datamodel.Pick.Cast(obj)

            # prevent a pick from myself from being repicked
            try:
                if pick.creationInfo().author() in ignoredAuthors:
                    continue
            except Exception:
                continue

            associated_picks.append(pick)
        return associated_picks

    def processOrigin(self, origin, event):
        """
        Process the given origin in the context of the given event.
        Used in both real-time and test mode.
        """
        if origin.creationInfo().agencyID() in self.ignoredAgencyIDs:
            return True

        originID = origin.publicID()
        eventID = event.publicID()

        # Ignore origins without any arrivals except if this origin
        # is explicitly white listed
        if origin.arrivalCount() == 0:
            if origin.creationInfo().agencyID() not in self.emptyOriginAgencyIDs:
                seiscomp.logging.debug(
                    "No arrivals in origin " + originID + " -> skipped")
                return

        seiscomp.logging.debug(
            "processing origin " + originID + " of event " + eventID)

        workspace = self.workspaces[event.publicID()]
        workspace.origin = origin
        workspace.new_picks.clear()

        # Find out which picks are new picks.
        #
        # Now all picks for which we don't have a DL pick are
        # considered new. Therefore also picks that were
        # received previously but failed to process due to
        # missing data are now re-processed.

        associated_picks = self.getAssociatedPicksForOrigin(originID)

        for pick in associated_picks:

            pickID = pick.publicID()
            if pickID not in workspace.all_picks:
                workspace.all_picks[pickID] = pick
                seiscomp.logging.debug("Added to workspace pick "+pickID)

            if _util.manual(pick) and not self.pickingConfig.repickManualPicks:
                seiscomp.logging.debug("Skipping manual pick "+pickID)
                continue

            # FIXME: The problem with this check at this point is that
            # workspace.mlpicks[pickID] will exist only if by the time
            # we perform this check, a repicker result for pickID is
            # already available, which may take minutes. If this is
            # not the case, redundant repickings cannot be avoided.
            if pickID in workspace.mlpicks:
                seiscomp.logging.debug("Skipping already repicked "+pickID)

            if pickID in workspace.attempted_picks:
                seiscomp.logging.debug(
                    "Skipping previously attempted repick "+pickID)
                continue

            if isRepick(pick, author=self.author()):
                seiscomp.logging.debug("Skipping repick "+pickID)
                continue

            found = False
            for _pickID in workspace.attempted_picks:
                _pick = workspace.attempted_picks[_pickID]
                if _pick.waveformID() == pick.waveformID():
                    found = True
                    break
            if found:
                seiscomp.logging.debug(
                    "Skipped previously attempted waveform ID of pick "+pickID)
                continue

            seiscomp.logging.debug("Adding new pick "+pickID)
            workspace.new_picks[pickID] = pick
            workspace.attempted_picks[pickID] = pick

        tmp = "%d" % len(workspace.new_picks) if workspace.new_picks else "no"
        seiscomp.logging.debug(tmp+" new picks")

        # Load additional waveforms
        waveforms = self._loadWaveformsForPicks(workspace.new_picks, event)
        for streamID in waveforms:
            workspace.waveforms[streamID] = waveforms[streamID]

        # #########################################################
        # Predict arrival times for unpicked stations
        # #########################################################

        if origin.creationInfo().agencyID() in emptyOriginAgencyIDs:
            maxDelta = 105.
        else:
            # The goal here is to limit the maximum distance to a
            # reasonable value reflecting the pick distribution
            # w.r.t. distance. E.g. if we have only regional picks
            # up to 8 degrees distance then we don't look at
            # teleseismic phases. Whereas if we have "a few"
            # teleseismic phases, there is a good chance to obtain
            # additional picks from all over the teleseismic
            # distance range (hard limit here: 105 degrees).
            maxDelta = 0.
            origin = workspace.origin
            # create a sorted list of distances of all *used* arrivals
            delta = []
            for i in range(origin.arrivalCount()):
                arr = origin.arrival(i)
                if arr.weight() < 0.5:
                    continue
                try:
                    delta.append(arr.distance())
                except ValueError:
                    continue
            delta.sort()

            # As maximum distance use the average distance
            # of the five farthest used arrivals.
            maxDelta = numpy.average(delta[-3:])

            # If the distance exceeds 50 degrees, it is promising
            # to look at the entire P distance range.
            if maxDelta > 40:
                maxDelta = 105

        if self.pickingConfig.tryUpickedStations:
            predictedPicks = self.findUnpickedStations(
                workspace.origin, maxDelta, workspace.all_picks)
            seiscomp.logging.debug("%d predicted picks" % len(predictedPicks))
            if len(predictedPicks) > 0:
                for pickID in predictedPicks:
                    pick = predictedPicks[pickID]
                    if pickID not in workspace.all_picks:
                        workspace.all_picks[pickID] = pick
                        if pickID in workspace.attempted_picks:
                            seiscomp.logging.debug(
                                "Skipping previously attempted repick "+pickID)
                            continue
                        workspace.new_picks[pickID] = pick
                        workspace.attempted_picks[pickID] = pick

                waveforms = self._loadWaveformsForPicks(
                    workspace.new_picks, event)
                for streamID in waveforms:
                    workspace.waveforms[streamID] = waveforms[streamID]

        # We dump the waveforms to files in order to
        # read them as miniSEED files into ObsPy. This
        # is the SeisComP-to-ObsPy iterface so to say.
        workspace.dump(eventRootDir=self.eventRootDir, spoolDir=self.spoolDir)

        # determine streams for which we don't have 3 components
        streamIDs = dict()
        for streamID in workspace.waveforms:
            firstRecord = workspace.waveforms[streamID][0]
            net, sta, loc, cha = _util.nslc(firstRecord)
            if (net, sta, loc) not in streamIDs:
                streamIDs[(net, sta, loc)] = []
            streamIDs[(net, sta, loc)].append(streamID)

        # for each complete NSL stream there should be 3 streamID's
        for nsl in streamIDs:
            if len(streamIDs[nsl]) < 3:
                for streamID in streamIDs[nsl]:
                    del workspace.waveforms[streamID]
                    seiscomp.logging.warning(
                        "Incomplete stream "+streamID+" ignored")

    def processEvent(self, event):
        """
        Processes the event and the current preferred origin.
        This is called only in real-time usage.
        """
        eventID = event.publicID()

        # Register an EventWorkspace instance if needed
        if eventID not in self.workspaces:
            self.workspaces[eventID] = _ews.EventWorkspace()
        workspace = self.workspaces[eventID]

        # Load a more complete version of the event
        event = self._loadEvent(eventID)
        workspace.event = event
        originID = event.preferredOriginID()
        if workspace.origin:
            if originID == workspace.origin.publicID():
                seiscomp.logging.debug(
                    "Event "+eventID+": no change of preferred origin")
                return
        origin = _dbutil.loadOrigin(self.query(), originID, full=True)
        self.processOrigin(origin, event)
        self.cleanup()

    def run(self):
        self.dumpConfiguration()

        try:
            eventID = self.commandline().optionString("event")
        except RuntimeError as e:
            # A bit strange exception, but we can't change it.
            assert str(e) == "Invalid type for cast"
            eventID = None

        if eventID:
            seiscomp.datamodel.PublicObject.SetRegistrationEnabled(False)
            seiscomp.logging.info("In single-event mode. Event is "+eventID)
            return self.testEvent(eventID)

        # enter real-time mode
        self.enableTimer(timeoutInterval)
        seiscomp.datamodel.PublicObject.SetRegistrationEnabled(False)

        return super().run()


def main():
    app = App(len(sys.argv), sys.argv)
    app()


if __name__ == "__main__":
    main()

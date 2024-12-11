#!/usr/bin/env seiscomp-python
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

"""
This is a very simple relocator that

- reads the preferred origin of an event from the database
- tries to find all matching DL picks based on predicted
  travel times
- relocates based on *only* the DL-picks
- sends the results
"""


import sys
import seiscomp.core
import seiscomp.client
import seiscomp.datamodel
import seiscomp.logging
import seiscomp.math
import seiscomp.seismology
import scdlpicker.dbutil as _dbutil
import scdlpicker.util as _util
import scdlpicker.relocation as _relocation
import scdlpicker.defaults as _defaults


class RelocatorApp(seiscomp.client.Application):

    def __init__(self, argc, argv):
        seiscomp.client.Application.__init__(self, argc, argv)
        self.setMessagingEnabled(True)
        self.setDatabaseEnabled(True, True)
        self.setLoadInventoryEnabled(True)
        self.setPrimaryMessagingGroup("LOCATION")

        self.allowedAuthorIDs = _defaults.allowedAuthorIDs

    def createCommandLineDescription(self):
        seiscomp.client.Application.createCommandLineDescription(self)
        self.commandline().addGroup("Config")
        self.commandline().addStringOption(
            "Config", "author",
            "Author of created objects")
        self.commandline().addStringOption(
            "Config", "agency",
            "Agency of created objects")
        self.commandline().addGroup("Target")
        self.commandline().addStringOption(
            "Target", "event,E",
            "load the specified event")
        self.commandline().addStringOption(
            "Target", "pick-authors",
            "space-separated whitelist of pick authors")
        self.commandline().addDoubleOption(
            "Target", "fixed-depth",
            "fix the depth at the specified value (in kilometers)")
        self.commandline().addDoubleOption(
            "Target", "max-residual",
            "limit the individual pick residual to the specified value "
            "(in seconds)")
        self.commandline().addDoubleOption(
            "Target", "max-rms",
            "limit the pick residual RMS to the specified value (in seconds)")
        self.commandline().addDoubleOption(
            "Target", "max-delta",
            "limit the station distance to the specified value (in degrees)")
        self.commandline().addOption(
            "Target", "test",
            "test mode - don't send the result")

    def init(self):
        if not super(RelocatorApp, self).init():
            return False

        self.inventory = seiscomp.client.Inventory.Instance().inventory()

        return True

    def _load(self, oid, tp):
        assert oid is not None
        obj = self.query().loadObject(tp.TypeInfo(), oid)
        tmp = tp.Cast(obj)
        return tmp


    def processEvent(self, eventID):
        seiscomp.logging.info("Working on event "+eventID)

        event  = _dbutil.loadEvent(self.query(), eventID, full=True)
        origin = _dbutil.loadOrigin(self.query(), event.preferredOriginID(), full=False)

        # Load all picks for a matching time span, independent of their
        # association.
        origin, picks = _dbutil.loadPicksForOrigin(
            self.query(),
            origin, self.inventory, self.allowedAuthorIDs, self.maxDelta)

        relocated = _relocation.relocate(
            origin, eventID, self.fixedDepth,
            self.minimumDepth, self.maxResidual)

        if not relocated:
            seiscomp.logging.info("No relocation result for event "+eventID)
            return

        now = seiscomp.core.Time.GMT()
        ci = _util.creationInfo(self.author, self.agencyID, now)
        relocated.setCreationInfo(ci)
        relocated.setEvaluationMode(seiscomp.datamodel.AUTOMATIC)

        ep = seiscomp.datamodel.EventParameters()
        seiscomp.datamodel.Notifier.Enable()
        ep.add(relocated)
        event.add(seiscomp.datamodel.OriginReference(relocated.publicID()))
        msg = seiscomp.datamodel.Notifier.GetMessage()
        seiscomp.datamodel.Notifier.Disable()

        if self.commandline().hasOption("test"):
            pass
        else:
            if self.connection().send(msg):
                seiscomp.logging.info("sent "+relocated.publicID())

        _util.summarize(relocated)

    def run(self):
        seiscomp.datamodel.PublicObject.SetRegistrationEnabled(True)

        try:
            pickAuthors = self.commandline().optionString("pick-authors")
            pickAuthors = pickAuthors.split()
        except RuntimeError:
            pickAuthors = ["dlpicker"]

        try:
            self.author = self.commandline().optionString("author")
        except RuntimeError:
            self.author = "dl-reloc"

        try:
            self.agencyID = self.commandline().optionString("agency")
        except RuntimeError:
            self.agencyID = "GFZ"

        try:
            self.fixedDepth = self.commandline().optionDouble("fixed-depth")
        except RuntimeError:
            self.fixedDepth = None

        eventIDs = self.commandline().optionString("event").split()
        for eventID in eventIDs:
            self.processEvent(eventID)

        return True


if __name__ == "__main__":
    app = RelocatorApp(len(sys.argv), sys.argv)
    status = app()
    sys.exit(status)

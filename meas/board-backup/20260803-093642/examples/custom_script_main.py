'''
Created on Nov 13, 2025

Sample boilerplate for custom scripts


@author: Pat Deegan
@copyright: Copyright (C) 2025 Pat Deegan, https://psychogenic.com
'''


### Memory protection ###
# Reading in a bunch of files tends to frag the memory
# so we can set a low threshold on the garbage collector
# here, and clean up post init
import gc
# stash the current value for garbage 
GCThreshold = gc.threshold()
# start very aggressive, to keep thing defragged
# as we read in ini and JSON files, etc
gc.threshold(80000)




### Logging ###
# uPython has no logging built-in, but we have an implementation 
# that behaves nicely here, if you want it
import ttboard.log as logging
logging.basicConfig(level=logging.DEBUG)


### TT demoboard ###
# this is what you need to detect the board/shuttle and 
# get access to the DemoBoard object
from ttboard.boot.demoboard_detect import DemoboardDetect
from ttboard.demoboard import DemoBoard
# load anything else you need...


# clean up memory
gc.collect()
# end by being so aggressive on collection
gc.threshold(GCThreshold)




### init code ###
# some globals
# logging handle
log = logging.getLogger(__name__)
# TT demoboard handle
tt = None # instantiate this after probing

# detect the board, carrier and shuttle
if DemoboardDetect.probe():
    log.info('Detected ' + DemoboardDetect.PCB_str())
else:
    log.error('Hm, could not figure out the DB/shuttle?')

# create and access the TT demoboard singleton
tt = DemoBoard.get()





### user code ###
# do whatever you like here
# say load a project, set input byte and start clocking

log.info("Loading factory test")
tt.shuttle.tt_um_factory_test.enable()
tt.ui_in = 1
tt.clock_project_PWM(1e6)   # clock it at 1M

print(tt.uo_out)






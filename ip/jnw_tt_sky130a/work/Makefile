
#- Core makefile provides the following commands:
# -----------------------------------------------------------
# ip: generate ../design/<LIB> from ../cic/ip.json
# gds: generate gds/<CELL>.gds
# cdl: generate cdl/<CELL>.cdl
# xlvs: Check cdl/<CELL>.cdl versus ../design/<LIB>/<CELL>.mag
# lpe: Extract parasitics lpe/<CELL>.spi
# drc: Run design rule checks on ../design/<LIB>/<CELL>.mag
# -----------------------------------------------------------


PREFIX=
LIB=JNW_TT_SKY130A
CELL=tt_um_jnw_wulffern

#- Provide cells you want to check for the lvsall drcall commands
CELLS = tt_um_jnw_wulffern

include ../tech/make/core.make

DATE = $(shell date "+%Y-%m-%d_%H%M")

deliver:
	-cp ../../../gds/${CELL}.gds ../../../gds/${DATE}_${CELL}.gds
	cp ant/${CELL}_ant.log ../../../reports/ant.log
	cp drc/${CELL}_drc.log ../../../reports/drc.log
	cp lvs/${CELL}_lvs.log ../../../reports/lvs.log
	cp lpe/${CELL}_lpe.spi ../../../spi
	cp xsch/${CELL}.spice ../../../spi
	cp lpe/${CELL}_lvs.log ../../../reports/lpe_lvs.log
	magic -noconsole -dnull deliver.tcl

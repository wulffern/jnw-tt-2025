

all: typical etc mc summary



TB=tran
VIEW=Sch
#VIEW=Lay
CELL=tt_um_jnw_wulffern
LIB =JNW_TT_SKY130A
OPT=

netlist_cell:
	test -d ../../work/xsch || mkdir ../../work/xsch
	cd ../../work/ && make xsch LIB=${LIB} CELL=${CELL}
ifeq ($(VIEW),Lay)
	-cd ../../work && make cdl lpe LIB=${LIB} CELL=${CELL}
endif

netlist: netlist_cell
#- I've seen port ordering change between xschem version, so I also
# generate a xdut.spi file that always has the right port ordering.
#- TODO: Should I add the view here? Either xsch or lpe??
	perl ../../tech/script/genxdut ../../work/xsch/${CELL}.spice ${CELL}






test:
	${MAKE} typical OPT="Debug"

typical: netlist
	cicsim run --replace replace.yaml --name ${VIEW}_typical ${TB} ${COPT} ${VIEW} Gt Ktt Tt Vt

slow: netlist
	cicsim run --replace replace.yaml --name ${VIEW}_slow ${TB} ${COPT} ${VIEW} Gt Kss "Tt" Vl

fast: netlist
	cicsim run --replace replace.yaml --name ${VIEW}_fast ${TB} ${COPT} ${VIEW} Gt Kff "Tt" Vh

tfs: netlist
	cicsim run --replace replace.yaml --name ${VIEW}_tfs ${TB} ${COPT} ${VIEW} Gt "Ktt,Kss,Kff" "Tt,Th,Tl" "Vt,Vl,Vh"

etc: netlist
	cicsim run --replace replace.yaml --name ${VIEW}_etc ${TB} ${COPT} ${VIEW} Gt "Kss,Kff,Ksf,Kfs" "Tt" "Vl,Vh"

mc: netlist
	cicsim run --replace replace.yaml --name ${VIEW}_mc --count 30 ${TB} ${COPT} ${VIEW} Gt "Kttmm" "Tt" "Vt"

ntc: netlist
	cicsim run --replace replace.yaml --name ${VIEW}_ntc ${TB} ${COPT} ${VIEW} Gt "Ktt,Kss,Kff" "Tt" Vt

temp: netlist
	cicsim run --replace replace.yaml --name ${VIEW}_temp ${TB} ${COPT} ${VIEW} Gt "Ktt" "Tt,Th,Tl" Vt

summary:
	cicsim summary --output "README.md"

slide:
	pandoc -s  -t slidy README.md -o README.html

clean:
	-rm -rf output_*
	-rm -rf __pycache__
	-rm *.run
	-rm *.pdf
	-rm *.csv

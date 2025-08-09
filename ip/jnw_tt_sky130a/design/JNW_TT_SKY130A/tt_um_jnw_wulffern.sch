v {xschem version=3.4.6 file_version=1.2}
G {}
K {}
V {}
S {}
E {}
P 4 1 -460 -250 {}
T {https://analogicus.github.io/jnw_gr07_sky130a/} -280 -710 0 0 0.4 0.4 {}
T {https://analogicus.github.io/jnw_gr06_sky130a/} -260 -350 0 0 0.4 0.4 {}
T {Temperature Sensors from NTNU Advanced Integrated Circuits Students} -810 -870 0 0 0.8 0.8 {}
N -380 -550 -220 -550 {lab=clk}
N -260 -570 -220 -570 {lab=VGND}
N -180 -200 -140 -200 {lab=VGND}
N -140 -210 -140 -200 {lab=VGND}
N -230 -250 -140 -250 {lab=VDPWR}
N -310 -590 -220 -590 {lab=VDPWR}
N -410 -230 -140 -230 {lab=RST}
N 80 -570 120 -570 {lab=#net1}
N 120 -570 170 -570 {lab=#net1}
N 160 -250 200 -250 {lab=#net2}
N 200 -250 250 -250 {lab=#net2}
N 250 -570 320 -570 {lab=uo_out[0]}
N 330 -250 400 -250 {lab=uo_out[1]}
N 290 -290 330 -290 {lab=VDPWR}
N 290 -210 330 -210 {lab=VGND}
N 210 -610 250 -610 {lab=VDPWR}
N 210 -530 250 -530 {lab=VGND}
N -240 -130 -200 -130 {lab=VDPWR}
N -450 -270 -410 -270 {lab=VDPWR}
N -240 -50 -200 -50 {lab=VGND}
N -310 -90 -280 -90 {lab=RST}
N -310 -230 -310 -90 {lab=RST}
N -200 -90 250 -90 {lab=#net3}
N 200 -110 250 -110 {lab=#net2}
N 200 -250 200 -110 {lab=#net2}
N 290 -60 290 -30 {lab=VGND}
N 290 -180 290 -140 {lab=VDPWR}
N 340 -100 410 -100 {lab=uo_out[2]}
N -550 -220 -490 -220 {lab=ui_in[0]}
N -450 -190 -450 -160 {lab=VGND}
C {JNW_GR06_SKY130A/JNW_GR06.sym} 10 -230 0 0 {name=x1}
C {JNW_GR07_SKY130A/JNW_GR07.sym} -70 -580 0 0 {name=x2}
C {devices/ipin.sym} -720 -670 0 0 {name=p1 lab=VDPWR}
C {devices/ipin.sym} -710 -90 0 0 {name=p2 lab=VGND}
C {devices/ipin.sym} -720 -590 0 0 {name=p3 lab=ui_in[7:0]}
C {devices/opin.sym} 610 -780 0 0 {name=p4 lab=uo_out[7:0]}
C {devices/ipin.sym} -720 -520 0 0 {name=p5 lab=uio_in[7:0]}
C {devices/opin.sym} 610 -740 0 0 {name=p6 lab=uio_out[7:0]}
C {devices/ipin.sym} -720 -450 0 0 {name=p7 lab=ua[7:0]}
C {devices/ipin.sym} -720 -400 0 0 {name=p8 lab=ena}
C {devices/ipin.sym} -380 -550 0 0 {name=p9 lab=clk}
C {devices/ipin.sym} -720 -300 0 0 {name=p10 lab=rst_n}
C {JNW_TR_SKY130A/JNWTR_BFX1_CV.sym} -490 -230 0 0 {name=x3 }
C {devices/lab_wire.sym} -290 -230 0 0 {name=p11 sig_type=std_logic lab=RST}
C {devices/lab_wire.sym} 320 -570 0 0 {name=p12 sig_type=std_logic lab=uo_out[0]}
C {devices/lab_wire.sym} 400 -250 0 0 {name=p13 sig_type=std_logic lab=uo_out[1]}
C {JNW_TR_SKY130A/JNWTR_BFX1_CV.sym} 250 -250 0 0 {name=x4 }
C {JNW_TR_SKY130A/JNWTR_BFX1_CV.sym} 170 -570 0 0 {name=x5 }
C {devices/lab_wire.sym} 330 -290 0 0 {name=p14 sig_type=std_logic lab=VDPWR}
C {devices/lab_wire.sym} 250 -610 0 0 {name=p15 sig_type=std_logic lab=VDPWR}
C {devices/lab_wire.sym} 330 -210 0 0 {name=p16 sig_type=std_logic lab=VGND}
C {devices/lab_wire.sym} 250 -530 0 0 {name=p17 sig_type=std_logic lab=VGND}
C {JNW_TR_SKY130A/JNWTR_IVX1_CV.sym} -280 -90 0 0 {name=x6 }
C {devices/lab_wire.sym} -200 -130 0 0 {name=p18 sig_type=std_logic lab=VDPWR}
C {devices/lab_wire.sym} -410 -270 0 0 {name=p19 sig_type=std_logic lab=VDPWR}
C {devices/lab_wire.sym} -200 -50 0 0 {name=p20 sig_type=std_logic lab=VGND}
C {JNW_TR_SKY130A/JNWTR_NRX1_CV.sym} 250 -90 0 0 {name=x7 }
C {devices/lab_wire.sym} 290 -30 0 0 {name=p21 sig_type=std_logic lab=VGND}
C {devices/lab_wire.sym} 290 -180 0 0 {name=p22 sig_type=std_logic lab=VDPWR}
C {devices/lab_wire.sym} 410 -100 0 0 {name=p23 sig_type=std_logic lab=uo_out[2]}
C {devices/lab_wire.sym} -450 -160 0 0 {name=p24 sig_type=std_logic lab=VGND}
C {devices/lab_wire.sym} -550 -220 0 0 {name=p25 sig_type=std_logic lab=ui_in[0]}
C {devices/lab_wire.sym} -310 -590 0 0 {name=p26 sig_type=std_logic lab=VDPWR}
C {devices/lab_wire.sym} -260 -570 0 0 {name=p27 sig_type=std_logic lab=VGND}
C {devices/lab_wire.sym} -230 -250 0 0 {name=p28 sig_type=std_logic lab=VDPWR}
C {devices/lab_wire.sym} -180 -200 0 0 {name=p29 sig_type=std_logic lab=VGND}
C {cborder/border_xs.sym} -340 40 0 0 {user="AIC Students" company="NTNU"}

v {xschem version=3.4.7 file_version=1.2
* opamp_two_stage -- two-stage Miller-compensated operational amplifier
* (issue #17), implementing the topology decided in
* spec/decision-records/0001-topology-and-cl.md (DR-0001).
*
* All device sizes below are derived by gm/ID methodology from
* sim/gm-id-characterization/records/20260909-052956-79c6a45.md
* (typical corner, 27 C). The full derivation -- every target Vov, the
* gm/ID and ID/W row each W comes from, and the arithmetic -- is in
* design/opamp_sizing.md. Do not edit sizes here without updating that
* file: it is the citation trail CLAUDE.md's "gm/ID first" rule requires.
*
* ---------------------------------------------------------------- topology
* Exactly DR-0001: NMOS input pair, PMOS common-source output device with
* an NMOS current-sink load, no cascode anywhere, Miller compensation
* between the two gain stages.
*
*   Stage 1 (differential -> single-ended)
*     M1/M2   NMOS input differential pair. vinn drives M1 (the mirror's
*             diode side), vinp drives M2 (the stage output side), so the
*             two-stage cascade is non-inverting from vinp to vout.
*     M3/M4   PMOS current-mirror load, M3 diode-connected on node n1,
*             M4 the output-side leg driving n2.
*     M5      NMOS tail current source, gate = ibias.
*
*   Stage 2 (single-ended, Class-A)
*     M6      PMOS common-source gain device, gate = n2 (the stage-1
*             output), source = vdd, drain = vout.
*     M7      NMOS constant-current sink load, gate = ibias.
*
*   Compensation (between the two stages)
*     CC      MIM Miller capacitor from n2 (stage-1 output) to nz.
*     RZ      series nulling resistor from nz to vout. RZ ~ 1/gm6 moves the
*             feedforward right-half-plane zero out of the way; without it
*             this design would need gm6/gm1 >= 10 rather than the 8.5 it
*             has, i.e. ~20 % more output-stage current for the same phase
*             margin. RZ carries no DC current, so it does not enter the
*             operating point at all.
*
*   Bias
*     MB1     NMOS diode that turns the externally supplied reference
*             current on pin `ibias` into the gate voltage M5 and M7
*             mirror. MB1 : M5 : M7 = 1 : 1 : 6 by finger count (all three
*             are 3 um/finger, L = 2 um), so with IBIAS = 10 uA the tail
*             carries 10 uA and the output stage 60 uA.
*
* --------------------------------------------------------- nominal design
*   IBIAS  = 10 uA (external; supplied by the testbench through `ibias`)
*   Itail  = 10 uA, ID(M1) = ID(M2) = 5 uA, I(M6) = I(M7) = 60 uA
*   Iq     = 80 uA total -> 264 uW at VDD = 3.3 V
*   CL     = 2 pF  [DR-1]  (external, not drawn here)
*   CC     = 0.619 pF (17.4 x 17.4 um MIM), RZ = 2.11 kohm ~ 1/gm6
*   gm1    = 60.3 uS, gm6 = 510 uS  (gm/ID-predicted, typical/27 C)
*   -> GBW ~ 15.5 MHz, second pole gm6/(2*pi*CL) ~ 40.6 MHz (2.6x GBW)
*   -> gm/gds-predicted open-loop DC gain ~ 97.7 dB (95.8 dB at the
*      simulated operating point -- design/opamp_sizing.md Sec.7)
* These are gm/ID predictions, not measured results: no AC/PVT testbench
* exists yet (tracker #7 item 5). The only simulation this schematic is
* committed against is the nominal DC operating-point check in
* design/check_dc_op.py.
*
* Systematic-offset (balance) condition, checked:
*   (W6/L6)/(W4/L4) = 72/6 = 12 = 2*(W7/L7)/(W5/L5) = 2*(36/2)/(6/2) = 12
* M4 and M6 share L = 1 um and M5/M7/MB1 share L = 2 um so the two mirror
* families track over corners.
*
* Pins: vdd, vss, vinp, vinn, vout, ibias
}
G {}
K {}
V {}
S {}
E {}
T {gf180-opamp: two-stage Miller-compensated op-amp (DR-0001), sized from the gm/ID record -- see design/opamp_sizing.md} -700 -520 0 0 0.4 0.4 {}
T {stage 1: NMOS pair + PMOS mirror load} -260 -420 0 0 0.3 0.3 {}
T {stage 2: PMOS CS + NMOS sink} 330 -420 0 0 0.3 0.3 {}
T {Miller compensation} 180 -230 0 0 0.3 0.3 {}
T {bias mirror 1:1:6} -560 30 0 0 0.3 0.3 {}
C {iopin.sym} -700 -300 0 0 {name=p1 lab=vdd}
C {iopin.sym} -700 -250 0 0 {name=p2 lab=vss}
C {iopin.sym} -700 -200 0 0 {name=p3 lab=vinp}
C {iopin.sym} -700 -150 0 0 {name=p4 lab=vinn}
C {iopin.sym} -700 -100 0 0 {name=p5 lab=vout}
C {iopin.sym} -700 -50 0 0 {name=p6 lab=ibias}
C {symbols/nfet_03v3.sym} -500 100 0 0 {name=MB1 model=nfet_03v3 W=6u L=2u nf=2 m=1}
N -480 70 -480 50 {}
C {lab_pin.sym} -480 50 0 0 {name=lb1 lab=ibias}
N -520 100 -540 100 {}
C {lab_pin.sym} -540 100 0 0 {name=lb2 lab=ibias}
N -480 130 -480 150 {}
C {lab_pin.sym} -480 150 0 0 {name=lb3 lab=vss}
N -480 100 -460 100 {}
C {lab_pin.sym} -460 100 0 0 {name=lb4 lab=vss}
C {symbols/nfet_03v3.sym} -50 100 0 0 {name=M5 model=nfet_03v3 W=6u L=2u nf=2 m=1}
N -30 70 -30 50 {}
C {lab_pin.sym} -30 50 0 0 {name=l51 lab=tail}
N -70 100 -90 100 {}
C {lab_pin.sym} -90 100 0 0 {name=l52 lab=ibias}
N -30 130 -30 150 {}
C {lab_pin.sym} -30 150 0 0 {name=l53 lab=vss}
N -30 100 -10 100 {}
C {lab_pin.sym} -10 100 0 0 {name=l54 lab=vss}
C {symbols/nfet_03v3.sym} -200 -100 0 0 {name=M1 model=nfet_03v3 W=3.6u L=1u nf=2 m=1}
N -180 -130 -180 -150 {}
C {lab_pin.sym} -180 -150 0 0 {name=l11 lab=n1}
N -220 -100 -240 -100 {}
C {lab_pin.sym} -240 -100 0 0 {name=l12 lab=vinn}
N -180 -70 -180 -50 {}
C {lab_pin.sym} -180 -50 0 0 {name=l13 lab=tail}
N -180 -100 -160 -100 {}
C {lab_pin.sym} -160 -100 0 0 {name=l14 lab=vss}
C {symbols/nfet_03v3.sym} 100 -100 0 0 {name=M2 model=nfet_03v3 W=3.6u L=1u nf=2 m=1}
N 120 -130 120 -150 {}
C {lab_pin.sym} 120 -150 0 0 {name=l21 lab=n2}
N 80 -100 60 -100 {}
C {lab_pin.sym} 60 -100 0 0 {name=l22 lab=vinp}
N 120 -70 120 -50 {}
C {lab_pin.sym} 120 -50 0 0 {name=l23 lab=tail}
N 120 -100 140 -100 {}
C {lab_pin.sym} 140 -100 0 0 {name=l24 lab=vss}
C {symbols/pfet_03v3.sym} -200 -300 0 0 {name=M3 model=pfet_03v3 W=6u L=1u nf=2 m=1}
N -180 -330 -180 -350 {}
C {lab_pin.sym} -180 -350 0 0 {name=l31 lab=vdd}
N -220 -300 -240 -300 {}
C {lab_pin.sym} -240 -300 0 0 {name=l32 lab=n1}
N -180 -270 -180 -250 {}
C {lab_pin.sym} -180 -250 0 0 {name=l33 lab=n1}
N -180 -300 -160 -300 {}
C {lab_pin.sym} -160 -300 0 0 {name=l34 lab=vdd}
C {symbols/pfet_03v3.sym} 100 -300 0 0 {name=M4 model=pfet_03v3 W=6u L=1u nf=2 m=1}
N 120 -330 120 -350 {}
C {lab_pin.sym} 120 -350 0 0 {name=l41 lab=vdd}
N 80 -300 60 -300 {}
C {lab_pin.sym} 60 -300 0 0 {name=l42 lab=n1}
N 120 -270 120 -250 {}
C {lab_pin.sym} 120 -250 0 0 {name=l43 lab=n2}
N 120 -300 140 -300 {}
C {lab_pin.sym} 140 -300 0 0 {name=l44 lab=vdd}
C {symbols/pfet_03v3.sym} 400 -300 0 0 {name=M6 model=pfet_03v3 W=72u L=1u nf=24 m=1}
N 420 -330 420 -350 {}
C {lab_pin.sym} 420 -350 0 0 {name=l61 lab=vdd}
N 380 -300 360 -300 {}
C {lab_pin.sym} 360 -300 0 0 {name=l62 lab=n2}
N 420 -270 420 -250 {}
C {lab_pin.sym} 420 -250 0 0 {name=l63 lab=vout}
N 420 -300 440 -300 {}
C {lab_pin.sym} 440 -300 0 0 {name=l64 lab=vdd}
C {symbols/nfet_03v3.sym} 400 0 0 0 {name=M7 model=nfet_03v3 W=36u L=2u nf=12 m=1}
N 420 -30 420 -50 {}
C {lab_pin.sym} 420 -50 0 0 {name=l71 lab=vout}
N 380 0 360 0 {}
C {lab_pin.sym} 360 0 0 0 {name=l72 lab=ibias}
N 420 30 420 50 {}
C {lab_pin.sym} 420 50 0 0 {name=l73 lab=vss}
N 420 0 440 0 {}
C {lab_pin.sym} 440 0 0 0 {name=l74 lab=vss}
C {symbols/cap_mim_2f0fF.sym} 250 -150 0 0 {name=CC model=cap_mim_2f0_m4m5_noshield W=17.4u L=17.4u m=1}
N 250 -180 250 -200 {}
C {lab_pin.sym} 250 -200 0 0 {name=lc1 lab=n2}
N 250 -120 250 -100 {}
C {lab_pin.sym} 250 -100 0 0 {name=lc2 lab=nz}
C {symbols/ppolyf_u_1k.sym} 250 0 0 0 {name=RZ model=ppolyf_u_1k W=2u L=4u m=1}
N 250 -30 250 -50 {}
C {lab_pin.sym} 250 -50 0 0 {name=lr1 lab=nz}
N 250 30 250 50 {}
C {lab_pin.sym} 250 50 0 0 {name=lr2 lab=vout}
N 230 0 210 0 {}
C {lab_pin.sym} 210 0 0 0 {name=lr3 lab=vss}

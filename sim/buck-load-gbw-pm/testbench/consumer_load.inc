* consumer_load.inc -- the gf180-buck error-amplifier load of record, for the
* Stage A probe of spec/decision-records/0008 (issue #295).
*
* SOURCE (read-only, copied verbatim, ideal parts, "nothing here is measured"):
*   2AMLogic/gf180-buck  design/netlists/compensator.spice  (type-III, Venable)
*   read 2026-11-10 at gf180-buck main b671d9e38321873caa9574f553d05473844c8bfc;
*   identical to the network quoted in DR-0008 section 2.1 (read 2026-11-06).
*   Original nodes: VOUT=vbuck, FBN=fbn, VC=vc, GND=0.
*
*   Rtop VOUT FBN 10k ; R3 VOUT X 204 ; C3 X FBN 780p ; Rbot FBN GND 5k
*   R2 FBN Z 4.55k ; C1 Z VC 1.75n ; C2 FBN VC 35.7p
*   XEA VDD33 GND VREF FBN VC IBIAS opamp_two_stage   (the DUT, instantiated
*   by each bench as Xdut, never here)
*
* A changed consumer netlist reopens DR-0008 section 2.1; it is not silently
* followed. Re-read the source before re-running (record states the sha).

.subckt ea_load vbuck fbn vc
Rtop vbuck fbn 10k
R3 vbuck x 204
C3 x fbn 780p
Rbot fbn 0 5k
R2 fbn z 4.55k
C1 z vc 1.75n
C2 fbn vc 35.7p
.ends ea_load

* PWM-comparator input on VC (DR-0008 item C3, consumer has not confirmed a
* value). The consumer's pwm_comp.spice XCPM1 (nfet_03v3 L=1u W=8u, gate = VC)
* is the only thing VC drives there. Its drain/source/bulk are tied to ground
* here, so this is the gate-channel + overlap capacitance of the device at the
* VC bias, an APPROXIMATION of the comparator input (in the real circuit D is a
* diode-connected PMOS node and S the tail node). Status: assumed.
.subckt pwm_in vc
XCPM1 0 vc 0 0 nfet_03v3 L=1u W=8u nf=1 ad='int((nf+1)/2) * W/nf * 0.18u' as='int((nf+2)/2) * W/nf * 0.18u' pd='2*int((nf+1)/2) * (W/nf + 0.18u)' ps='2*int((nf+2)/2) * (W/nf + 0.18u)' nrd='0.18u / W' nrs='0.18u / W' sa=0 sb=0 sd=0 m=1
.ends pwm_in

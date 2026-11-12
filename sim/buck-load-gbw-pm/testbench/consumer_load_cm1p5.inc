* consumer_load_cm1p5.inc -- the gf180-buck error-amplifier load at the NEW
* error-amplifier common mode (1.50 V), for the Stage A re-run of issue #295.
*
* SOURCE (read-only): 2AMLogic/gf180-buck PR #132 (closes gf180-buck#131), merge
*   commit 003fe17f85ca35fc63c3f9835404b2aa8135951a, decision record
*   spec/decision-records/0008-ea-input-common-mode.md section 4: Vref = FBN =
*   1.50 V at DC; "Rtop/Rbot 10 kOhm / 50 kOhm (was 5 kOhm)" with every
*   compensator value unchanged (R2 4.55k, R3 204, C1 1.75n, C2 35.7p, C3 780p).
*   The consumer's design/netlists/compensator.spice at that sha still shows
*   Rbot 5k (the DR says it edits no netlist), so the DR text is the source of
*   Rbot = 50k, not the netlist. Consumer numbers are its analysis, not measured.
*   Buck output node: 1.50 V * (1 + 10k/50k) = 1.80 V, unchanged.
*   The 0.6 V network stays in consumer_load.inc (its record is untouched).
*
* A changed consumer netlist reopens DR-0008 section 2.1; it is not silently
* followed. Re-read the source before re-running (record states the sha).

.subckt ea_load vbuck fbn vc
Rtop vbuck fbn 10k
R3 vbuck x 204
C3 x fbn 780p
Rbot fbn 0 50k
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

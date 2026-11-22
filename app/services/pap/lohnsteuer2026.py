"""Lohnsteuerberechnung nach dem amtlichen Programmablaufplan Lohnsteuer2026.

Automatisch erzeugt mit ``tools/pap_codegen.py`` aus
``data/pap/Lohnsteuer2026.xml`` – nicht von Hand ändern, sondern neu erzeugen.
"""

# ruff: noqa: E501

from __future__ import annotations

from app.services.pap.runtime import BD, ROUND_DOWN, ROUND_UP  # noqa: F401

PAP_NAME = 'Lohnsteuer2026'
PAP_VERSION = '1.0'
SOURCE_FILE = 'Lohnsteuer2026.xml'
SOURCE_SHA256 = '63d8981646d139eba2f4dd990c13b43c4fb3883b402a5a40cddf253aa7aa96b4'

INPUTS: dict[str, str] = {
    'af': 'int',
    'AJAHR': 'int',
    'ALTER1': 'int',
    'ALV': 'int',
    'f': 'double',
    'JFREIB': 'BigDecimal',
    'JHINZU': 'BigDecimal',
    'JRE4': 'BigDecimal',
    'JRE4ENT': 'BigDecimal',
    'JVBEZ': 'BigDecimal',
    'KRV': 'int',
    'KVZ': 'BigDecimal',
    'LZZ': 'int',
    'LZZFREIB': 'BigDecimal',
    'LZZHINZU': 'BigDecimal',
    'MBV': 'BigDecimal',
    'PKPV': 'BigDecimal',
    'PKPVAGZ': 'BigDecimal',
    'PKV': 'int',
    'PVA': 'BigDecimal',
    'PVS': 'int',
    'PVZ': 'int',
    'R': 'int',
    'RE4': 'BigDecimal',
    'SONSTB': 'BigDecimal',
    'SONSTENT': 'BigDecimal',
    'STERBE': 'BigDecimal',
    'STKL': 'int',
    'VBEZ': 'BigDecimal',
    'VBEZM': 'BigDecimal',
    'VBEZS': 'BigDecimal',
    'VBS': 'BigDecimal',
    'VJAHR': 'int',
    'ZKF': 'BigDecimal',
    'ZMVB': 'int',
}

OUTPUTS: dict[str, tuple[str, ...]] = {
    'STANDARD': ('BK', 'BKS', 'LSTLZZ', 'SOLZLZZ', 'SOLZS', 'STS',),
    'DBA': ('VFRB', 'VFRBS1', 'VFRBS2', 'WVFRB', 'WVFRBO', 'WVFRBM',),
}


class Lohnsteuer2026:
    """PAP Lohnsteuer2026: Eingaben in Cent bzw. als Merker, Ausgaben in Cent."""

    TAB1 = [BD.ZERO, BD.valueOf( 0.4), BD.valueOf( 0.384), BD.valueOf( 0.368), BD.valueOf( 0.352), BD.valueOf( 0.336), BD.valueOf( 0.32), BD.valueOf( 0.304), BD.valueOf( 0.288), BD.valueOf( 0.272), BD.valueOf( 0.256), BD.valueOf( 0.24), BD.valueOf( 0.224), BD.valueOf( 0.208), BD.valueOf( 0.192), BD.valueOf( 0.176), BD.valueOf( 0.16), BD.valueOf( 0.152), BD.valueOf( 0.144), BD.valueOf( 0.14), BD.valueOf( 0.136), BD.valueOf( 0.132), BD.valueOf( 0.128), BD.valueOf( 0.124), BD.valueOf( 0.12), BD.valueOf( 0.116), BD.valueOf( 0.112), BD.valueOf( 0.108), BD.valueOf( 0.104), BD.valueOf( 0.1), BD.valueOf( 0.096), BD.valueOf( 0.092), BD.valueOf( 0.088), BD.valueOf( 0.084), BD.valueOf( 0.08), BD.valueOf( 0.076), BD.valueOf( 0.072), BD.valueOf( 0.068), BD.valueOf( 0.064), BD.valueOf( 0.06), BD.valueOf( 0.056), BD.valueOf( 0.052), BD.valueOf( 0.048), BD.valueOf( 0.044), BD.valueOf( 0.04), BD.valueOf( 0.036), BD.valueOf( 0.032), BD.valueOf( 0.028), BD.valueOf( 0.024), BD.valueOf( 0.02), BD.valueOf( 0.016), BD.valueOf( 0.012), BD.valueOf( 0.008), BD.valueOf( 0.004), BD.valueOf( 0)]
    TAB2 = [BD.ZERO, BD.valueOf( 3000), BD.valueOf( 2880), BD.valueOf( 2760), BD.valueOf( 2640), BD.valueOf( 2520), BD.valueOf( 2400), BD.valueOf( 2280), BD.valueOf( 2160), BD.valueOf( 2040), BD.valueOf( 1920), BD.valueOf( 1800), BD.valueOf( 1680), BD.valueOf( 1560), BD.valueOf( 1440), BD.valueOf( 1320), BD.valueOf( 1200), BD.valueOf( 1140), BD.valueOf( 1080), BD.valueOf( 1050), BD.valueOf( 1020), BD.valueOf( 990), BD.valueOf( 960), BD.valueOf( 930), BD.valueOf( 900), BD.valueOf( 870), BD.valueOf( 840), BD.valueOf( 810), BD.valueOf( 780), BD.valueOf( 750), BD.valueOf( 720), BD.valueOf( 690), BD.valueOf( 660), BD.valueOf( 630), BD.valueOf( 600), BD.valueOf( 570), BD.valueOf( 540), BD.valueOf( 510), BD.valueOf( 480), BD.valueOf( 450), BD.valueOf( 420), BD.valueOf( 390), BD.valueOf( 360), BD.valueOf( 330), BD.valueOf( 300), BD.valueOf( 270), BD.valueOf( 240), BD.valueOf( 210), BD.valueOf( 180), BD.valueOf( 150), BD.valueOf( 120), BD.valueOf( 90), BD.valueOf( 60), BD.valueOf( 30), BD.valueOf( 0)]
    TAB3 = [BD.ZERO, BD.valueOf( 900), BD.valueOf( 864), BD.valueOf( 828), BD.valueOf( 792), BD.valueOf( 756), BD.valueOf( 720), BD.valueOf( 684), BD.valueOf( 648), BD.valueOf( 612), BD.valueOf( 576), BD.valueOf( 540), BD.valueOf( 504), BD.valueOf( 468), BD.valueOf( 432), BD.valueOf( 396), BD.valueOf( 360), BD.valueOf( 342), BD.valueOf( 324), BD.valueOf( 315), BD.valueOf( 306), BD.valueOf( 297), BD.valueOf( 288), BD.valueOf( 279), BD.valueOf( 270), BD.valueOf( 261), BD.valueOf( 252), BD.valueOf( 243), BD.valueOf( 234), BD.valueOf( 225), BD.valueOf( 216), BD.valueOf( 207), BD.valueOf( 198), BD.valueOf( 189), BD.valueOf( 180), BD.valueOf( 171), BD.valueOf( 162), BD.valueOf( 153), BD.valueOf( 144), BD.valueOf( 135), BD.valueOf( 126), BD.valueOf( 117), BD.valueOf( 108), BD.valueOf( 99), BD.valueOf( 90), BD.valueOf( 81), BD.valueOf( 72), BD.valueOf( 63), BD.valueOf( 54), BD.valueOf( 45), BD.valueOf( 36), BD.valueOf( 27), BD.valueOf( 18), BD.valueOf( 9), BD.valueOf( 0)]
    TAB4 = [BD.ZERO, BD.valueOf( 0.4), BD.valueOf( 0.384), BD.valueOf( 0.368), BD.valueOf( 0.352), BD.valueOf( 0.336), BD.valueOf( 0.32), BD.valueOf( 0.304), BD.valueOf( 0.288), BD.valueOf( 0.272), BD.valueOf( 0.256), BD.valueOf( 0.24), BD.valueOf( 0.224), BD.valueOf( 0.208), BD.valueOf( 0.192), BD.valueOf( 0.176), BD.valueOf( 0.16), BD.valueOf( 0.152), BD.valueOf( 0.144), BD.valueOf( 0.14), BD.valueOf( 0.136), BD.valueOf( 0.132), BD.valueOf( 0.128), BD.valueOf( 0.124), BD.valueOf( 0.12), BD.valueOf( 0.116), BD.valueOf( 0.112), BD.valueOf( 0.108), BD.valueOf( 0.104), BD.valueOf( 0.1), BD.valueOf( 0.096), BD.valueOf( 0.092), BD.valueOf( 0.088), BD.valueOf( 0.084), BD.valueOf( 0.08), BD.valueOf( 0.076), BD.valueOf( 0.072), BD.valueOf( 0.068), BD.valueOf( 0.064), BD.valueOf( 0.06), BD.valueOf( 0.056), BD.valueOf( 0.052), BD.valueOf( 0.048), BD.valueOf( 0.044), BD.valueOf( 0.04), BD.valueOf( 0.036), BD.valueOf( 0.032), BD.valueOf( 0.028), BD.valueOf( 0.024), BD.valueOf( 0.02), BD.valueOf( 0.016), BD.valueOf( 0.012), BD.valueOf( 0.008), BD.valueOf( 0.004), BD.valueOf( 0)]
    TAB5 = [BD.ZERO, BD.valueOf( 1900), BD.valueOf( 1824), BD.valueOf( 1748), BD.valueOf( 1672), BD.valueOf( 1596), BD.valueOf( 1520), BD.valueOf( 1444), BD.valueOf( 1368), BD.valueOf( 1292), BD.valueOf( 1216), BD.valueOf( 1140), BD.valueOf( 1064), BD.valueOf( 988), BD.valueOf( 912), BD.valueOf( 836), BD.valueOf( 760), BD.valueOf( 722), BD.valueOf( 684), BD.valueOf( 665), BD.valueOf( 646), BD.valueOf( 627), BD.valueOf( 608), BD.valueOf( 589), BD.valueOf( 570), BD.valueOf( 551), BD.valueOf( 532), BD.valueOf( 513), BD.valueOf( 494), BD.valueOf( 475), BD.valueOf( 456), BD.valueOf( 437), BD.valueOf( 418), BD.valueOf( 399), BD.valueOf( 380), BD.valueOf( 361), BD.valueOf( 342), BD.valueOf( 323), BD.valueOf( 304), BD.valueOf( 285), BD.valueOf( 266), BD.valueOf( 247), BD.valueOf( 228), BD.valueOf( 209), BD.valueOf( 190), BD.valueOf( 171), BD.valueOf( 152), BD.valueOf( 133), BD.valueOf( 114), BD.valueOf( 95), BD.valueOf( 76), BD.valueOf( 57), BD.valueOf( 38), BD.valueOf( 19), BD.valueOf( 0)]
    ZAHL1 = BD.ONE
    ZAHL2 = BD.valueOf(2)
    ZAHL5 = BD.valueOf(5)
    ZAHL7 = BD.valueOf(7)
    ZAHL12 = BD.valueOf(12)
    ZAHL100 = BD.valueOf(100)
    ZAHL360 = BD.valueOf(360)
    ZAHL500 = BD.valueOf(500)
    ZAHL700 = BD.valueOf(700)
    ZAHL1000 = BD.valueOf(1000)
    ZAHL10000 = BD.valueOf(10000)

    def __init__(self, **inputs: object) -> None:
        self.af = 1
        self.AJAHR = 0
        self.ALTER1 = 0
        self.ALV = 0
        self.f = 1.0
        self.JFREIB = BD.ZERO
        self.JHINZU = BD.ZERO
        self.JRE4 = BD.ZERO
        self.JRE4ENT = BD.ZERO
        self.JVBEZ = BD.ZERO
        self.KRV = 0
        self.KVZ = BD.ZERO
        self.LZZ = 1
        self.LZZFREIB = BD.ZERO
        self.LZZHINZU = BD.ZERO
        self.MBV = BD.ZERO
        self.PKPV = BD.ZERO
        self.PKPVAGZ = BD.ZERO
        self.PKV = 0
        self.PVA = BD.ZERO
        self.PVS = 0
        self.PVZ = 0
        self.R = 0
        self.RE4 = BD.ZERO
        self.SONSTB = BD.ZERO
        self.SONSTENT = BD.ZERO
        self.STERBE = BD.ZERO
        self.STKL = 1
        self.VBEZ = BD.ZERO
        self.VBEZM = BD.ZERO
        self.VBEZS = BD.ZERO
        self.VBS = BD.ZERO
        self.VJAHR = 0
        self.ZKF = BD.ZERO
        self.ZMVB = 0
        self.BK = BD.ZERO
        self.BKS = BD.ZERO
        self.LSTLZZ = BD.ZERO
        self.SOLZLZZ = BD.ZERO
        self.SOLZS = BD.ZERO
        self.STS = BD.ZERO
        self.VFRB = BD.ZERO
        self.VFRBS1 = BD.ZERO
        self.VFRBS2 = BD.ZERO
        self.WVFRB = BD.ZERO
        self.WVFRBO = BD.ZERO
        self.WVFRBM = BD.ZERO
        self.ALTE = BD.ZERO
        self.ANP = BD.ZERO
        self.ANTEIL1 = BD.ZERO
        self.AVSATZAN = BD.ZERO
        self.BBGKVPV = BD.ZERO
        self.BBGRVALV = BD.ZERO
        self.BMG = BD.ZERO
        self.DIFF = BD.ZERO
        self.EFA = BD.ZERO
        self.FVB = BD.ZERO
        self.FVBSO = BD.ZERO
        self.FVBZ = BD.ZERO
        self.FVBZSO = BD.ZERO
        self.GFB = BD.ZERO
        self.HBALTE = BD.ZERO
        self.HFVB = BD.ZERO
        self.HFVBZ = BD.ZERO
        self.HFVBZSO = BD.ZERO
        self.HOCH = BD.ZERO
        self.J = 0
        self.JBMG = BD.ZERO
        self.JLFREIB = BD.ZERO
        self.JLHINZU = BD.ZERO
        self.JW = BD.ZERO
        self.K = 0
        self.KFB = BD.ZERO
        self.KVSATZAN = BD.ZERO
        self.KZTAB = 0
        self.LSTJAHR = BD.ZERO
        self.LSTOSO = BD.ZERO
        self.LSTSO = BD.ZERO
        self.MIST = BD.ZERO
        self.PKPVAGZJ = BD.ZERO
        self.PVSATZAN = BD.ZERO
        self.RVSATZAN = BD.ZERO
        self.RW = BD.ZERO
        self.SAP = BD.ZERO
        self.SOLZFREI = BD.ZERO
        self.SOLZJ = BD.ZERO
        self.SOLZMIN = BD.ZERO
        self.SOLZSBMG = BD.ZERO
        self.SOLZSZVE = BD.ZERO
        self.ST = BD.ZERO
        self.ST1 = BD.ZERO
        self.ST2 = BD.ZERO
        self.VBEZB = BD.ZERO
        self.VBEZBSO = BD.ZERO
        self.VERGL = BD.ZERO
        self.VSPHB = BD.ZERO
        self.VSP = BD.ZERO
        self.VSPN = BD.ZERO
        self.VSPALV = BD.ZERO
        self.VSPKVPV = BD.ZERO
        self.VSPR = BD.ZERO
        self.W1STKL5 = BD.ZERO
        self.W2STKL5 = BD.ZERO
        self.W3STKL5 = BD.ZERO
        self.X = BD.ZERO
        self.Y = BD.ZERO
        self.ZRE4 = BD.ZERO
        self.ZRE4J = BD.ZERO
        self.ZRE4VP = BD.ZERO
        self.ZRE4VPR = BD.ZERO
        self.ZTABFB = BD.ZERO
        self.ZVBEZ = BD.ZERO
        self.ZVBEZJ = BD.ZERO
        self.ZVE = BD.ZERO
        self.ZX = BD.ZERO
        self.ZZX = BD.ZERO
        for name, value in inputs.items():
            if name not in INPUTS:
                raise ValueError(f"Unbekannter PAP-Eingabeparameter: {name}")
            setattr(self, name, _coerce(INPUTS[name], value))

    def calculate(self) -> dict[str, int]:
        """Führt den PAP aus und liefert alle Ausgaben in Cent."""
        self.MAIN()
        return {
            name: int(getattr(self, name).value)
            for group in OUTPUTS.values()
            for name in group
        }

    def MAIN(self) -> None:
        self.MPARA()
        self.MRE4JL()
        self.VBEZBSO = BD.ZERO
        self.MRE4()
        self.MRE4ABZ()
        self.MBERECH()
        self.MSONST()

    def MPARA(self) -> None:
        """Zuweisung von Werten für bestimmte Steuer- und Sozialversicherungsparameter PAP Seite 14"""
        self.BBGRVALV = BD.valueOf(101400)
        self.AVSATZAN = BD.valueOf(0.013)
        self.RVSATZAN = BD.valueOf(0.093)
        self.BBGKVPV = BD.valueOf(69750)
        self.KVSATZAN = (self.KVZ.divide(self.ZAHL2).divide(self.ZAHL100)).add(BD.valueOf(0.07))
        if self.PVS == 1:
            self.PVSATZAN = BD.valueOf(0.023)
        else:
            self.PVSATZAN = BD.valueOf(0.018)
        if self.PVZ == 1:
            self.PVSATZAN = self.PVSATZAN.add(BD.valueOf(0.006))
        else:
            self.PVSATZAN = self.PVSATZAN.subtract(self.PVA.multiply(BD.valueOf(0.0025)))
        self.W1STKL5 = BD.valueOf(14071)
        self.W2STKL5 = BD.valueOf(34939)
        self.W3STKL5 = BD.valueOf(222260)
        self.GFB = BD.valueOf(12348)
        self.SOLZFREI = BD.valueOf(20350)

    def MRE4JL(self) -> None:
        """Ermittlung des Jahresarbeitslohns nach § 39 b Absatz 2 Satz 2 EStG, PAP Seite 15"""
        if self.LZZ == 1:
            self.ZRE4J = self.RE4.divide (self.ZAHL100, 2, ROUND_DOWN)
            self.ZVBEZJ = self.VBEZ.divide (self.ZAHL100, 2, ROUND_DOWN)
            self.JLFREIB = self.LZZFREIB.divide (self.ZAHL100, 2, ROUND_DOWN)
            self.JLHINZU = self.LZZHINZU.divide (self.ZAHL100, 2, ROUND_DOWN)
        else:
            if self.LZZ == 2:
                self.ZRE4J = (self.RE4.multiply (self.ZAHL12)).divide (self.ZAHL100, 2, ROUND_DOWN)
                self.ZVBEZJ = (self.VBEZ.multiply (self.ZAHL12)).divide (self.ZAHL100, 2, ROUND_DOWN)
                self.JLFREIB = (self.LZZFREIB.multiply (self.ZAHL12)).divide (self.ZAHL100, 2, ROUND_DOWN)
                self.JLHINZU = (self.LZZHINZU.multiply (self.ZAHL12)).divide (self.ZAHL100, 2, ROUND_DOWN)
            else:
                if self.LZZ == 3:
                    self.ZRE4J = (self.RE4.multiply (self.ZAHL360)).divide (self.ZAHL700, 2, ROUND_DOWN)
                    self.ZVBEZJ = (self.VBEZ.multiply (self.ZAHL360)).divide (self.ZAHL700, 2, ROUND_DOWN)
                    self.JLFREIB = (self.LZZFREIB.multiply (self.ZAHL360)).divide (self.ZAHL700, 2, ROUND_DOWN)
                    self.JLHINZU = (self.LZZHINZU.multiply (self.ZAHL360)).divide (self.ZAHL700, 2, ROUND_DOWN)
                else:
                    self.ZRE4J = (self.RE4.multiply (self.ZAHL360)).divide (self.ZAHL100, 2, ROUND_DOWN)
                    self.ZVBEZJ = (self.VBEZ.multiply (self.ZAHL360)).divide (self.ZAHL100, 2, ROUND_DOWN)
                    self.JLFREIB = (self.LZZFREIB.multiply (self.ZAHL360)).divide (self.ZAHL100, 2, ROUND_DOWN)
                    self.JLHINZU = (self.LZZHINZU.multiply (self.ZAHL360)).divide (self.ZAHL100, 2, ROUND_DOWN)
        if self.af == 0:
            self.f = 1

    def MRE4(self) -> None:
        """Freibeträge für Versorgungsbezüge, Altersentlastungsbetrag (§ 39b Absatz 2 Satz 3 EStG), PAP Seite 16"""
        if self.ZVBEZJ.compareTo (BD.ZERO) == 0:
            self.FVBZ = BD.ZERO
            self.FVB = BD.ZERO
            self.FVBZSO = BD.ZERO
            self.FVBSO = BD.ZERO
        else:
            if self.VJAHR < 2006:
                self.J = 1
            else:
                if self.VJAHR < 2058:
                    self.J = self.VJAHR - 2004
                else:
                    self.J = 54
            if self.LZZ == 1:
                self.VBEZB = (self.VBEZM.multiply (BD.valueOf(self.ZMVB))).add (self.VBEZS)
                self.HFVB = self.TAB2[self.J].divide (self.ZAHL12).multiply (BD.valueOf(self.ZMVB)).setScale (0, ROUND_UP)
                self.FVBZ = self.TAB3[self.J].divide (self.ZAHL12).multiply (BD.valueOf(self.ZMVB)).setScale (0, ROUND_UP)
            else:
                self.VBEZB = ((self.VBEZM.multiply (self.ZAHL12)).add (self.VBEZS)).setScale (2, ROUND_DOWN)
                self.HFVB = self.TAB2[self.J]
                self.FVBZ = self.TAB3[self.J]
            self.FVB = ((self.VBEZB.multiply (self.TAB1[self.J]))).divide (self.ZAHL100).setScale (2, ROUND_UP)
            if self.FVB.compareTo (self.HFVB) == 1:
                self.FVB = self.HFVB
            if self.FVB.compareTo (self.ZVBEZJ) == 1:
                self.FVB = self.ZVBEZJ
            self.FVBSO = (self.FVB.add((self.VBEZBSO.multiply (self.TAB1[self.J])).divide (self.ZAHL100))).setScale (2, ROUND_UP)
            if self.FVBSO.compareTo (self.TAB2[self.J]) == 1:
                self.FVBSO = self.TAB2[self.J]
            self.HFVBZSO = (((self.VBEZB.add(self.VBEZBSO)).divide (self.ZAHL100)).subtract (self.FVBSO)).setScale (2, ROUND_DOWN)
            self.FVBZSO = (self.FVBZ.add((self.VBEZBSO).divide (self.ZAHL100))).setScale (0, ROUND_UP)
            if self.FVBZSO.compareTo (self.HFVBZSO) == 1:
                self.FVBZSO = self.HFVBZSO.setScale(0, ROUND_UP)
            if self.FVBZSO.compareTo (self.TAB3[self.J]) == 1:
                self.FVBZSO = self.TAB3[self.J]
            self.HFVBZ = ((self.VBEZB.divide (self.ZAHL100)).subtract (self.FVB)).setScale (2, ROUND_DOWN)
            if self.FVBZ.compareTo (self.HFVBZ) == 1:
                self.FVBZ = self.HFVBZ.setScale (0, ROUND_UP)
        self.MRE4ALTE()

    def MRE4ALTE(self) -> None:
        """Altersentlastungsbetrag (§ 39b Absatz 2 Satz 3 EStG), PAP Seite 17"""
        if self.ALTER1 == 0:
            self.ALTE = BD.ZERO
        else:
            if self.AJAHR < 2006:
                self.K = 1
            else:
                if self.AJAHR < 2058:
                    self.K = self.AJAHR - 2004
                else:
                    self.K = 54
            self.BMG = self.ZRE4J.subtract (self.ZVBEZJ)
            self.ALTE = (self.BMG.multiply(self.TAB4[self.K])).setScale(0, ROUND_UP)
            self.HBALTE = self.TAB5[self.K]
            if self.ALTE.compareTo (self.HBALTE) == 1:
                self.ALTE = self.HBALTE

    def MRE4ABZ(self) -> None:
        """Ermittlung des Jahresarbeitslohns nach Abzug der Freibeträge nach § 39 b Absatz 2 Satz 3 und 4 EStG, PAP Seite 20"""
        self.ZRE4 = (self.ZRE4J.subtract (self.FVB).subtract (self.ALTE).subtract (self.JLFREIB).add (self.JLHINZU)).setScale (2, ROUND_DOWN)
        if self.ZRE4.compareTo (BD.ZERO) == -1:
            self.ZRE4 = BD.ZERO
        self.ZRE4VP = self.ZRE4J
        self.ZVBEZ = self.ZVBEZJ.subtract(self.FVB).setScale(2, ROUND_DOWN)
        if self.ZVBEZ.compareTo(BD.ZERO) == -1:
            self.ZVBEZ = BD.ZERO

    def MBERECH(self) -> None:
        """Berechnung fuer laufende Lohnzahlungszeitraueme Seite 21"""
        self.MZTABFB()
        self.VFRB = ((self.ANP.add(self.FVB.add(self.FVBZ))).multiply(self.ZAHL100)).setScale(0, ROUND_DOWN)
        self.MLSTJAHR()
        self.WVFRB = ((self.ZVE.subtract(self.GFB)).multiply(self.ZAHL100)).setScale(0, ROUND_DOWN)
        if self.WVFRB.compareTo(BD.ZERO) == -1:
            self.WVFRB = BD.ZERO
        self.LSTJAHR = (self.ST.multiply(BD.valueOf(self.f))).setScale(0,ROUND_DOWN)
        self.UPLSTLZZ()
        if self.ZKF.compareTo(BD.ZERO) == 1:
            self.ZTABFB = self.ZTABFB.add(self.KFB)
            self.MRE4ABZ()
            self.MLSTJAHR()
            self.JBMG = (self.ST.multiply(BD.valueOf(self.f))).setScale(0,ROUND_DOWN)
        else:
            self.JBMG = self.LSTJAHR
        self.MSOLZ()

    def MZTABFB(self) -> None:
        """Ermittlung der festen Tabellenfreibeträge (ohne Vorsorgepauschale), PAP Seite 22"""
        self.ANP = BD.ZERO
        if self.ZVBEZ.compareTo (BD.ZERO) >= 0  and  self.ZVBEZ.compareTo(self.FVBZ) == -1:
            self.FVBZ = BD.valueOf(self.ZVBEZ.longValue())
        if self.STKL < 6:
            if self.ZVBEZ.compareTo (BD.ZERO) == 1:
                if (self.ZVBEZ.subtract (self.FVBZ)).compareTo (BD.valueOf(102)) == -1:
                    self.ANP = (self.ZVBEZ.subtract (self.FVBZ)).setScale (0, ROUND_UP)
                else:
                    self.ANP = BD.valueOf(102)
        else:
            self.FVBZ = BD.ZERO
            self.FVBZSO = BD.ZERO
        if self.STKL < 6:
            if self.ZRE4.compareTo(self.ZVBEZ) == 1:
                if self.ZRE4.subtract(self.ZVBEZ).compareTo(BD.valueOf(1230)) == -1:
                    self.ANP = self.ANP.add(self.ZRE4).subtract(self.ZVBEZ).setScale(0,ROUND_UP)
                else:
                    self.ANP = self.ANP.add(BD.valueOf(1230))
        self.KZTAB = 1
        if self.STKL == 1:
            self.SAP = BD.valueOf(36)
            self.KFB = (self.ZKF.multiply (BD.valueOf(9756))).setScale (0, ROUND_DOWN)
        else:
            if self.STKL == 2:
                self.EFA = BD.valueOf(4260)
                self.SAP = BD.valueOf(36)
                self.KFB = (self.ZKF.multiply (BD.valueOf(9756))).setScale (0, ROUND_DOWN)
            else:
                if self.STKL == 3:
                    self.KZTAB = 2
                    self.SAP = BD.valueOf(36)
                    self.KFB = (self.ZKF.multiply (BD.valueOf(9756))).setScale (0, ROUND_DOWN)
                else:
                    if self.STKL == 4:
                        self.SAP = BD.valueOf(36)
                        self.KFB = (self.ZKF.multiply (BD.valueOf(4878))).setScale (0, ROUND_DOWN)
                    else:
                        if self.STKL == 5:
                            self.SAP = BD.valueOf(36)
                            self.KFB = BD.ZERO
                        else:
                            self.KFB = BD.ZERO
        self.ZTABFB = (self.EFA.add (self.ANP).add (self.SAP).add (self.FVBZ)).setScale (2, ROUND_DOWN)

    def MLSTJAHR(self) -> None:
        """Ermittlung Jahreslohnsteuer, PAP Seite 23"""
        self.UPEVP()
        self.ZVE = self.ZRE4.subtract (self.ZTABFB).subtract(self.VSP)
        self.UPMLST()

    def UPLSTLZZ(self) -> None:
        """PAP Seite 24"""
        self.JW = self.LSTJAHR.multiply(self.ZAHL100)
        self.UPANTEIL()
        self.LSTLZZ = self.ANTEIL1

    def UPMLST(self) -> None:
        """PAP Seite 25"""
        if self.ZVE.compareTo (self.ZAHL1) == -1:
            self.ZVE = BD.ZERO
            self.X = BD.ZERO
        else:
            self.X = (self.ZVE.divide (BD.valueOf(self.KZTAB))).setScale (0, ROUND_DOWN)
        if self.STKL < 5:
            self.UPTAB26()
        else:
            self.MST5_6()

    def UPEVP(self) -> None:
        """Vorsorgepauschale (§ 39b Absatz 2 Satz 5 Nummer 3 EStG) PAP Seite 26"""
        if self.KRV == 1:
            self.VSPR = BD.ZERO
        else:
            if self.ZRE4VP.compareTo(self.BBGRVALV) == 1:
                self.ZRE4VPR = self.BBGRVALV
            else:
                self.ZRE4VPR = self.ZRE4VP
            self.VSPR = (self.ZRE4VPR.multiply(self.RVSATZAN)).setScale(2,ROUND_DOWN)
        self.MVSPKVPV()
        if self.ALV == 1:
            pass
        else:
            if self.STKL == 6:
                pass
            else:
                self.MVSPHB()

    def MVSPKVPV(self) -> None:
        """Vorsorgepauschale (§ 39b Absatz 2 Satz 5 Nummer 3 Buchstaben b bis d EStG), PAP Seite 27"""
        if self.ZRE4VP.compareTo(self.BBGKVPV) == 1:
            self.ZRE4VPR = self.BBGKVPV
        else:
            self.ZRE4VPR = self.ZRE4VP
        if self.PKV > 0:
            if self.STKL == 6:
                self.VSPKVPV = BD.ZERO
            else:
                self.PKPVAGZJ = self.PKPVAGZ.multiply(self.ZAHL12).divide(self.ZAHL100).setScale(2,ROUND_DOWN)
                self.VSPKVPV = self.PKPV.multiply(self.ZAHL12).divide(self.ZAHL100).setScale(2, ROUND_DOWN)
                self.VSPKVPV = self.VSPKVPV.subtract(self.PKPVAGZJ)
                if self.VSPKVPV.compareTo(BD.ZERO) == -1:
                    self.VSPKVPV = BD.ZERO
        else:
            self.VSPKVPV = self.ZRE4VPR.multiply(self.KVSATZAN.add(self.PVSATZAN)).setScale(2, ROUND_DOWN)
        self.VSP = self.VSPKVPV.add(self.VSPR).setScale(0, ROUND_UP)

    def MVSPHB(self) -> None:
        """Höchstbetragsberechnung zur Arbeitslosenversicherung (§ 39b Absatz 2 Satz 5 Nummer 3 Buchstabe e EStG), PAP Seite 28"""
        if self.ZRE4VP.compareTo(self.BBGRVALV) == 1:
            self.ZRE4VPR = self.BBGRVALV
        else:
            self.ZRE4VPR = self.ZRE4VP
        self.VSPALV = self.AVSATZAN.multiply(self.ZRE4VPR).setScale(2, ROUND_DOWN)
        self.VSPHB = self.VSPALV.add(self.VSPKVPV).setScale(2, ROUND_DOWN)
        if self.VSPHB.compareTo(BD.valueOf(1900)) == 1:
            self.VSPHB = BD.valueOf(1900)
        self.VSPN = self.VSPR.add(self.VSPHB).setScale(0, ROUND_UP)
        if self.VSPN.compareTo(self.VSP) == 1:
            self.VSP = self.VSPN

    def MST5_6(self) -> None:
        """Lohnsteuer fuer die Steuerklassen V und VI (§ 39b Absatz 2 Satz 7 EStG), PAP Seite 29"""
        self.ZZX = self.X
        if self.ZZX.compareTo(self.W2STKL5) == 1:
            self.ZX = self.W2STKL5
            self.UP5_6()
            if self.ZZX.compareTo (self.W3STKL5) == 1:
                self.ST = (self.ST.add ((self.W3STKL5.subtract (self.W2STKL5)).multiply (BD.valueOf(0.42)))).setScale (0, ROUND_DOWN)
                self.ST = (self.ST.add ((self.ZZX.subtract (self.W3STKL5)).multiply (BD.valueOf(0.45)))).setScale (0, ROUND_DOWN)
            else:
                self.ST = (self.ST.add ((self.ZZX.subtract (self.W2STKL5)).multiply (BD.valueOf(0.42)))).setScale (0, ROUND_DOWN)
        else:
            self.ZX = self.ZZX
            self.UP5_6()
            if self.ZZX.compareTo (self.W1STKL5) == 1:
                self.VERGL = self.ST
                self.ZX = self.W1STKL5
                self.UP5_6()
                self.HOCH = (self.ST.add ((self.ZZX.subtract (self.W1STKL5)).multiply (BD.valueOf(0.42)))).setScale (0, ROUND_DOWN)
                if self.HOCH.compareTo (self.VERGL) == -1:
                    self.ST = self.HOCH
                else:
                    self.ST = self.VERGL

    def UP5_6(self) -> None:
        """Unterprogramm zur Lohnsteuer fuer die Steuerklassen V und VI (§ 39b Absatz 2 Satz 7 EStG), PAP Seite 30"""
        self.X = (self.ZX.multiply (BD.valueOf(1.25))).setScale (0, ROUND_DOWN)
        self.UPTAB26()
        self.ST1 = self.ST
        self.X = (self.ZX.multiply (BD.valueOf(0.75))).setScale (0, ROUND_DOWN)
        self.UPTAB26()
        self.ST2 = self.ST
        self.DIFF = (self.ST1.subtract (self.ST2)).multiply (self.ZAHL2)
        self.MIST = (self.ZX.multiply (BD.valueOf(0.14))).setScale (0, ROUND_DOWN)
        if self.MIST.compareTo (self.DIFF) == 1:
            self.ST = self.MIST
        else:
            self.ST = self.DIFF

    def MSOLZ(self) -> None:
        """Solidaritätszuschlag, PAP Seite 31"""
        self.SOLZFREI = (self.SOLZFREI.multiply(BD.valueOf(self.KZTAB)))
        if self.JBMG.compareTo (self.SOLZFREI) == 1:
            self.SOLZJ = (self.JBMG.multiply (BD.valueOf(5.5))).divide(self.ZAHL100).setScale(2, ROUND_DOWN)
            self.SOLZMIN = (self.JBMG.subtract (self.SOLZFREI)).multiply (BD.valueOf(11.9)).divide (self.ZAHL100).setScale (2, ROUND_DOWN)
            if self.SOLZMIN.compareTo (self.SOLZJ) == -1:
                self.SOLZJ = self.SOLZMIN
            self.JW = self.SOLZJ.multiply (self.ZAHL100).setScale (0, ROUND_DOWN)
            self.UPANTEIL()
            self.SOLZLZZ = self.ANTEIL1
        else:
            self.SOLZLZZ = BD.ZERO
        if self.R > 0:
            self.JW = self.JBMG.multiply (self.ZAHL100)
            self.UPANTEIL()
            self.BK = self.ANTEIL1
        else:
            self.BK = BD.ZERO

    def UPANTEIL(self) -> None:
        """Anteil von Jahresbeträgen fuer einen LZZ (§ 39b Absatz 2 Satz 9 EStG), PAP Seite 32"""
        if self.LZZ == 1:
            self.ANTEIL1 = self.JW
        else:
            if self.LZZ == 2:
                self.ANTEIL1 = self.JW.divide (self.ZAHL12, 0, ROUND_DOWN)
            else:
                if self.LZZ == 3:
                    self.ANTEIL1 = (self.JW.multiply (self.ZAHL7)).divide (self.ZAHL360, 0, ROUND_DOWN)
                else:
                    self.ANTEIL1 = self.JW.divide (self.ZAHL360, 0, ROUND_DOWN)

    def MSONST(self) -> None:
        """Berechnung sonstiger Bezüge nach § 39b Absatz 3 Sätze 1 bis 8 EStG, PAP Seite 33"""
        self.LZZ = 1
        if self.ZMVB == 0:
            self.ZMVB = 12
        if self.SONSTB.compareTo (BD.ZERO) == 0  and  self.MBV.compareTo (BD.ZERO) == 0:
            self.LSTSO = BD.ZERO
            self.STS = BD.ZERO
            self.SOLZS = BD.ZERO
            self.BKS = BD.ZERO
        else:
            self.MOSONST()
            self.ZRE4J = ((self.JRE4.add (self.SONSTB)).divide (self.ZAHL100)).setScale (2, ROUND_DOWN)
            self.ZVBEZJ = ((self.JVBEZ.add (self.VBS)).divide (self.ZAHL100)).setScale (2, ROUND_DOWN)
            self.VBEZBSO = self.STERBE
            self.MRE4SONST()
            self.MLSTJAHR()
            self.WVFRBM = (self.ZVE.subtract(self.GFB)).multiply(self.ZAHL100).setScale(2,ROUND_DOWN)
            if self.WVFRBM.compareTo(BD.ZERO) == -1:
                self.WVFRBM = BD.ZERO
            self.LSTSO = self.ST.multiply (self.ZAHL100)
            self.STS = self.LSTSO.subtract(self.LSTOSO).multiply(BD.valueOf(self.f)).divide(self.ZAHL100, 0, ROUND_DOWN).multiply(self.ZAHL100)
            self.STSMIN()

    def STSMIN(self) -> None:
        """PAP Seite 34"""
        if self.STS.compareTo(BD.ZERO) == -1:
            if self.MBV.compareTo(BD.ZERO) == 0:
                pass
            else:
                self.LSTLZZ = self.LSTLZZ.add(self.STS)
                if self.LSTLZZ.compareTo(BD.ZERO) == -1:
                    self.LSTLZZ = BD.ZERO
                self.SOLZLZZ = self.SOLZLZZ.add(self.STS.multiply(BD.valueOf(5.5).divide(self.ZAHL100))).setScale(0, ROUND_DOWN)
                if self.SOLZLZZ.compareTo(BD.ZERO) == -1:
                    self.SOLZLZZ = BD.ZERO
                self.BK = self.BK.add(self.STS)
                if self.BK.compareTo(BD.ZERO) == -1:
                    self.BK = BD.ZERO
            self.STS = BD.ZERO
            self.SOLZS = BD.ZERO
        else:
            self.MSOLZSTS()
        if self.R > 0:
            self.BKS = self.STS
        else:
            self.BKS = BD.ZERO

    def MSOLZSTS(self) -> None:
        """Berechnung des SolZ auf sonstige Bezüge, PAP Seite 35"""
        if self.ZKF.compareTo(BD.ZERO) == 1:
            self.SOLZSZVE = self.ZVE.subtract(self.KFB)
        else:
            self.SOLZSZVE = self.ZVE
        if self.SOLZSZVE.compareTo(BD.ONE) == -1:
            self.SOLZSZVE = BD.ZERO
            self.X = BD.ZERO
        else:
            self.X = self.SOLZSZVE.divide(BD.valueOf(self.KZTAB), 0, ROUND_DOWN)
        if self.STKL < 5:
            self.UPTAB26()
        else:
            self.MST5_6()
        self.SOLZSBMG = self.ST.multiply(BD.valueOf(self.f)).setScale(0,ROUND_DOWN)
        if self.SOLZSBMG.compareTo(self.SOLZFREI) == 1:
            self.SOLZS = self.STS.multiply(BD.valueOf(5.5)).divide(self.ZAHL100, 0, ROUND_DOWN)
        else:
            self.SOLZS = BD.ZERO

    def MOSONST(self) -> None:
        """Sonderberechnung ohne sonstige Bezüge für Berechnung bei sonstigen Bezügen, PAP Seite 36"""
        self.ZRE4J = (self.JRE4.divide (self.ZAHL100)).setScale (2, ROUND_DOWN)
        self.ZVBEZJ = (self.JVBEZ.divide (self.ZAHL100)).setScale (2, ROUND_DOWN)
        self.JLFREIB = self.JFREIB.divide (self.ZAHL100, 2, ROUND_DOWN)
        self.JLHINZU = self.JHINZU.divide (self.ZAHL100, 2, ROUND_DOWN)
        self.MRE4()
        self.MRE4ABZ()
        self.ZRE4VP = self.ZRE4VP.subtract(self.JRE4ENT.divide(self.ZAHL100))
        self.MZTABFB()
        self.VFRBS1 = ((self.ANP.add(self.FVB.add(self.FVBZ))).multiply(self.ZAHL100)).setScale(2,ROUND_DOWN)
        self.MLSTJAHR()
        self.WVFRBO = ((self.ZVE.subtract(self.GFB)).multiply(self.ZAHL100)).setScale(2, ROUND_DOWN)
        if self.WVFRBO.compareTo(BD.ZERO) == -1:
            self.WVFRBO = BD.ZERO
        self.LSTOSO = self.ST.multiply (self.ZAHL100)

    def MRE4SONST(self) -> None:
        """Sonderberechnung mit sonstigen Bezüge für Berechnung bei sonstigen Bezügen, PAP Seite 37"""
        self.MRE4()
        self.FVB = self.FVBSO
        self.MRE4ABZ()
        self.ZRE4VP = self.ZRE4VP.add(self.MBV.divide(self.ZAHL100)).subtract(self.JRE4ENT.divide(self.ZAHL100)).subtract(self.SONSTENT.divide(self.ZAHL100))
        self.FVBZ = self.FVBZSO
        self.MZTABFB()
        self.VFRBS2 = ((((self.ANP.add(self.FVB).add(self.FVBZ))).multiply(self.ZAHL100))).subtract(self.VFRBS1)

    def UPTAB26(self) -> None:
        """Tarifliche Einkommensteuer §32a EStG, PAP Seite 38"""
        if self.X.compareTo(self.GFB.add(self.ZAHL1)) == -1:
            self.ST = BD.ZERO
        else:
            if self.X.compareTo (BD.valueOf(17800)) == -1:
                self.Y = (self.X.subtract(self.GFB)).divide(self.ZAHL10000, 6,ROUND_DOWN)
                self.RW = self.Y.multiply (BD.valueOf(914.51))
                self.RW = self.RW.add (BD.valueOf(1400))
                self.ST = (self.RW.multiply (self.Y)).setScale (0, ROUND_DOWN)
            else:
                if self.X.compareTo (BD.valueOf(69879)) == -1:
                    self.Y = (self.X.subtract (BD.valueOf(17799))).divide (self.ZAHL10000, 6, ROUND_DOWN)
                    self.RW = self.Y.multiply (BD.valueOf(173.1))
                    self.RW = self.RW.add (BD.valueOf(2397))
                    self.RW = self.RW.multiply (self.Y)
                    self.ST = (self.RW.add (BD.valueOf(1034.87))).setScale (0, ROUND_DOWN)
                else:
                    if self.X.compareTo (BD.valueOf(277826)) == -1:
                        self.ST = ((self.X.multiply (BD.valueOf(0.42))).subtract (BD.valueOf(11135.63))).setScale (0, ROUND_DOWN)
                    else:
                        self.ST = ((self.X.multiply (BD.valueOf(0.45))).subtract (BD.valueOf(19470.38))).setScale (0, ROUND_DOWN)
        self.ST = self.ST.multiply (BD.valueOf(self.KZTAB))


def _coerce(type_: str, value: object) -> object:
    if type_ == "int":
        return int(value)  # type: ignore[call-overload]
    if type_ == "double":
        return float(value)  # type: ignore[arg-type]
    return BD.valueOf(value)

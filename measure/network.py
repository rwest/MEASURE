#!/usr/bin/python
# -*- coding: utf-8 -*-

################################################################################
#
#   MEASURE - Master Equation Automatic Solver for Unimolecular REactions
#
#   Copyright (c) 2010 by Joshua W. Allen (jwallen@mit.edu)
#
#   Permission is hereby granted, free of charge, to any person obtaining a
#   copy of this software and associated documentation files (the 'Software'),
#   to deal in the Software without restriction, including without limitation
#   the rights to use, copy, modify, merge, publish, distribute, sublicense,
#   and/or sell copies of the Software, and to permit persons to whom the
#   Software is furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED 'AS IS', WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#   FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#   DEALINGS IN THE SOFTWARE.
#
################################################################################

"""
Contains classes that define an internal representation of a unimolecular
reaction network.
"""

import math
import numpy
import cython
import logging

import chempy.constants as constants
import chempy.states as states

from reaction import *
from collision import *

################################################################################

class NetworkError(Exception):
    """
    An exception raised while manipulating unimolecular reaction networks for
    any reason. Pass a string describing the cause of the exceptional behavior.
    """
    pass

################################################################################

class Network:
    """
    A representation of a unimolecular reaction network. The attributes are:

    =================== ======================= ================================
    Attribute           Type                    Description
    =================== ======================= ================================
    `isomers`           ``list``                A list of the unimolecular isomers in the network
    `reactants`         ``list``                A list of the bimolecular reactant channels in the network
    `products`          ``list``                A list of the bimolecular product channels in the network
    `pathReactions`     ``list``                A list of reaction objects that connect adjacent isomers (the high-pressure-limit)
    `bathGas`           :class:`Species`        The bath gas
    `collisionModel`    :class:`CollisionModel` The collision model to use
    `netReactions`      ``list``                A list of reaction objects that connect any pair of isomers
    =================== ======================= ================================

    """

    def __init__(self, isomers=None, reactants=None, products=None, pathReactions=None, bathGas=None):
        self.isomers = isomers or []
        self.reactants = reactants or []
        self.products = products or []
        self.pathReactions = pathReactions or []
        self.bathGas = bathGas
        self.netReactions = []
    
    def getEnergyGrains(self, Emin, Emax, dE=0.0, Ngrains=0):
        """
        Return an array of energy grains that have a minimum of `Emin`, a
        maximum of `Emax`, and either a spacing of `dE` or have number of
        grains `nGrains`. The first three parameters are in J/mol, as is the
        returned array of energy grains.
        """
        useGrainSize = False

        if Ngrains <= 0 and dE <= 0.0:
            # Neither grain size nor number of grains specified, so raise exception
            raise NetworkError('You must specify a positive value for either dE or Ngrains.')
        elif Ngrains <= 0 and dE > 0.0:
            # Only grain size was specified, so we must use it
            useGrainSize = True
        elif Ngrains > 0 and dE <= 0.0:
            # Only number of grains was specified, so we must use it
            useGrainSize = False
        else:
            # Both were specified, so we choose the tighter constraint
            # (i.e. the one that will give more grains, and so better accuracy)
            dE0 = (Emax - Emin) / (Ngrains - 1)
            useGrainSize = (dE0 > dE)
        
        # Generate the array of energies
        if useGrainSize:
            return numpy.arange(Emin, Emax + dE, dE, numpy.float64)
        else:
            return numpy.linspace(Emin, Emax, Ngrains, numpy.float64)
        
    def autoGenerateEnergyGrains(self, Tmax, grainSize=0.0, Ngrains=0):
        """
        Select a suitable list of energies to use for subsequent calculations.
        The procedure is:

        1. Calculate the equilibrium distribution of the highest-energy isomer
           at the largest temperature of interest (to get the broadest
           distribution)

        2. Calculate the energy at which the tail of the distribution is some
           fraction of the maximum

        3. Add the difference between the ground-state energy of the isomer and
           the highest ground-state energy in the system (either isomer or
           transition state)

        You must specify either the desired grain spacing `grainSize` in J/mol 
        or the desired number of grains `Ngrains`, as well as a temperature 
        `Tmax` in K to use for the equilibrium calculation, which should be the
        highest temperature of interest. You can specify both `grainSize` and 
        `Ngrains`, in which case the one that gives the more accurate result 
        will be used (i.e. they represent a maximum grain size and a minimum
        number of grains). An array containing the energy grains in J/mol is 
        returned.
        """
        
        if grainSize == 0.0 and Ngrains == 0:
            raise NetworkError('Must provide either grainSize or Ngrains parameter to Network.determineEnergyGrains().')

        # For the purposes of finding the maximum energy we will use 251 grains
        nE = 251; dE = 0.0

        # The minimum energy is the lowest isomer energy on the PES
        Emin = 1.0e25
        for species in self.isomers:
            if species.E0 < Emin: Emin = species.E0
        Emin = math.floor(Emin) # Round to nearest whole number

        # Determine the isomer with the maximum ground-state energy
        isomer = None
        for species in self.isomers:
            if isomer is None: isomer = species
            elif species.E0 > isomer.E0: isomer = species
        Emax0 = isomer.E0

        # (Try to) purposely overestimate Emax using arbitrary multiplier
        # to (hopefully) avoid multiple density of states calculations
        mult = 50
        done = False
        maxIter = 5
        iterCount = 0
        while not done and iterCount < maxIter:

            iterCount += 1
            
            Emax = math.ceil(Emax0 + mult * constants.R * Tmax)

            Elist = self.getEnergyGrains(0.0, Emax-Emin, dE, nE)
            densStates = isomer.states.getDensityOfStates(Elist)
            eqDist = densStates * numpy.exp(-Elist / constants.R / Tmax)
            eqDist /= numpy.sum(eqDist)
            
            # Find maximum of distribution
            maxIndex = eqDist.argmax()
            
            # If tail of distribution is much lower than the maximum, then we've found bounds for Emax
            tol = 1e-4
            if eqDist[-1] / eqDist[maxIndex] < tol:
                r = nE - 1
                while r > maxIndex and not done:
                    if eqDist[r] / eqDist[maxIndex] > tol: done = True
                    else: r -= 1
                Emax = Elist[r] + Emin
                # A final check to ensure we've captured almost all of the equilibrium distribution
                if abs(1.0 - numpy.sum(eqDist[0:r]) / numpy.sum(eqDist)) > tol:
                    done = False
                    mult += 50
            else:
                mult += 50

        # Add difference between isomer ground-state energy and highest
        # transition state or reactant channel energy
        Emax0_iso = Emin
        for species in self.reactants:
            E = sum([spec.E0 for spec in species])
            if Emax0_iso < E: Emax0_iso = E
        Emax0_rxn = Emin
        for rxn in self.pathReactions:
            if rxn.transitionState is not None:
                E = rxn.transitionState.E0
                if Emax0_rxn < E: Emax0_rxn = E
        Emax += max([isomer.E0, Emax0_iso, Emax0_rxn]) - isomer.E0

        # Round Emax up to nearest integer
        Emax = math.ceil(Emax)

        # Return the chosen energy grains
        return self.getEnergyGrains(Emin, Emax, grainSize, Ngrains)

    def calculateDensitiesOfStates(self, Elist, E0):
        """
        Calculate and return an array containing the density of states for each
        isomer and reactant channel in the network. `Elist` represents the
        array of energies in J/mol at which to compute each density of states.
        The ground-state energies `E0` in J/mol are used to shift each density
        of states for each configuration to the same zero of energy. The 
        returned density of states is in units of mol/J.
        """
        
        Ngrains = len(Elist)
        Nisom = len(self.isomers)
        Nreac = len(self.reactants)
        densStates = numpy.zeros((Nisom+Nreac, Ngrains), numpy.float64)
        dE = Elist[1] - Elist[0]
        
        logging.info('Calculating densities of states...')
        
        # Densities of states for isomers
        for i in range(Nisom):
            logging.info('Calculating density of states for isomer "%s"' % self.isomers[i])
            densStates0 = self.isomers[i].states.getDensityOfStates(Elist)
            # Shift to common zero of energy
            r0 = int(round(E0[i] / dE))
            densStates[i,r0:] = densStates0[:-r0+len(densStates0)]
        
        # Densities of states for reactant channels
        for n in range(Nreac):
            if self.reactants[n][0].states is not None and self.reactants[n][1].states is not None:
                logging.debug('Calculating density of states for reactant channel "%s"' % (' + '.join([str(spec) for spec in self.reactants[n]])))
                densStates0 = self.reactants[n][0].states.getDensityOfStates(Elist)
                densStates1 = self.reactants[n][1].states.getDensityOfStates(Elist)
                densStates0 = states.convolve(densStates0, densStates1, Elist)
                # Shift to common zero of energy
                r0 = int(round(E0[n+Nisom] / dE))
                densStates[n+Nisom,r0:] = densStates0[:-r0+len(densStates0)]
            else:
                logging.debug('NOT calculating density of states for reactant channel "%s"' % (' + '.join([str(spec) for spec in self.reactants[n]])))
        logging.debug('')
        
        return densStates

    def calculateMicrocanonicalRates(self, Elist, densStates, T=None):
        """
        Calculate and return arrays containing the microcanonical rate 
        coefficients :math:`k(E)` for the isomerization, dissociation, and
        association path reactions in the network. `Elist` represents the
        array of energies in J/mol at which to compute each density of states,
        while `densStates` represents the density of states of each isomer and
        reactant channel in mol/J.
        """
        
        Ngrains = len(Elist)
        Nisom = len(self.isomers)
        Nreac = len(self.reactants)
        Nprod = len(self.products)
        
        Kij = numpy.zeros([Nisom,Nisom,Ngrains], numpy.float64)
        Gnj = numpy.zeros([Nreac+Nprod,Nisom,Ngrains], numpy.float64)
        Fim = numpy.zeros([Nisom,Nreac,Ngrains], numpy.float64)
        
        logging.info('Calculating microcanonical rate coefficients k(E)...')
        
        for rxn in self.pathReactions:
            if rxn.reactants[0] in self.isomers and rxn.products[0] in self.isomers:
                # Isomerization
                reac = self.isomers.index(rxn.reactants[0])
                prod = self.isomers.index(rxn.products[0])
                Kij[prod,reac,:], Kij[reac,prod,:] = calculateMicrocanonicalRateCoefficient(rxn, Elist, densStates[reac,:], densStates[prod,:], T)
            elif rxn.reactants[0] in self.isomers and rxn.products in self.reactants:
                # Dissociation (reversible)
                reac = self.isomers.index(rxn.reactants[0])
                prod = self.reactants.index(rxn.products)
                Gnj[prod,reac,:], Fim[reac,prod,:] = calculateMicrocanonicalRateCoefficient(rxn, Elist, densStates[reac,:], densStates[prod+Nisom,:], T)
            elif rxn.reactants[0] in self.isomers and rxn.products in self.products:
                # Dissociation (irreversible)
                reac = self.isomers.index(rxn.reactants[0])
                prod = self.products.index(rxn.products) + Nreac
                Gnj[prod,reac,:], dummy = calculateMicrocanonicalRateCoefficient(rxn, Elist, densStates[reac,:], None, T)
            elif rxn.reactants in self.reactants and rxn.products[0] in self.isomers:
                # Association
                reac = self.reactants.index(rxn.reactants)
                prod = self.isomers.index(rxn.products[0])
                Fim[prod,reac,:], Gnj[reac,prod,:] = calculateMicrocanonicalRateCoefficient(rxn, Elist, densStates[reac+Nisom,:], densStates[prod,:], T)
            else:
                raise NetworkError('Unexpected type of path reaction "%s"' % rxn)
        logging.debug('')
        
        return Kij, Gnj, Fim
        
    def calculateRateCoefficients(self, Tlist, Plist, Elist, method):
        """
        Calculate the phenomenological rate coefficients :math:`k(T,P)` for the
        network at the given temperatures `Tlist` in K and pressures `Plist` in
        Pa. The `method` string is used to indicate the method to use, and
        should be one of "modified strong collision", "reservoir state", or
        "chemically-significant eigenvalues".
        """

        # Determine the values of some counters
        Ngrains = len(Elist)
        Nisom = len(self.isomers)
        Nreac = len(self.reactants)
        Nprod = len(self.products)
        dE = Elist[1] - Elist[0]
        
        # Get ground-state energies of all isomers and each reactant channel
        # that has the necessary parameters
        # An exception will be raised if a unimolecular isomer is missing
        # this information
        E0 = numpy.zeros((Nisom+Nreac), numpy.float64)
        for i in range(Nisom):
            E0[i] = self.isomers[i].E0
        for n in range(Nreac):
            E0[n+Nisom] = sum([spec.E0 for spec in self.reactants[n]])
        
        # Get first reactive grain for each isomer
        Ereac = numpy.ones(Nisom, numpy.float64) * 1e20
        for i in range(Nisom):
            for rxn in self.pathReactions:
                if rxn.reactants[0] == self.isomers[i] or rxn.products[0] == self.isomers[i]:
                    if rxn.transitionState.E0 < Ereac[i]: 
                        Ereac[i] = rxn.transitionState.E0
        
        # Shift energy grains such that lowest is zero
        Emin = Elist[0]
        for rxn in self.pathReactions:
            rxn.transitionState.E0 -= Emin
        E0 -= Emin
        Ereac -= Emin
        Elist -= Emin

        # Calculate density of states for each isomer and each reactant channel
        # that has the necessary parameters
        densStates0 = self.calculateDensitiesOfStates(Elist, E0)

        K = numpy.zeros((len(Tlist),len(Plist),Nisom+Nreac+Nprod,Nisom+Nreac+Nprod), numpy.float64)
        
        for t, T in enumerate(Tlist):
            
            # Calculate microcanonical rate coefficients for each path reaction
            # If degree of freedom data is provided for the transition state, then RRKM theory is used
            # If high-pressure limit Arrhenius data is provided, then the inverse Laplace transform method is used
            # Otherwise an exception is raised
            # This is only dependent on temperature for the ILT method with
            # certain Arrhenius parameters
            Kij, Gnj, Fim = self.calculateMicrocanonicalRates(Elist, densStates0, T)

            # Rescale densities of states such that, when they are integrated
            # using the Boltzmann factor as a weighting factor, the result is unity
            densStates = numpy.zeros_like(densStates0)
            eqRatios = numpy.zeros(Nisom+Nreac, numpy.float64)
            for i in range(Nisom+Nreac):
                eqRatios[i] = numpy.sum(densStates0[i,:] * numpy.exp(-Elist / constants.R / T)) * dE
                densStates[i,:] = densStates0[i,:] / eqRatios[i] * dE
        
            for p, P in enumerate(Plist):
                
                logging.info('Calculating k(T,P) values at %g K, %g bar...' % (T, P/1e5))
                
                # Calculate collision frequencies
                collFreq = numpy.zeros(Nisom, numpy.float64)
                for i in range(Nisom):
                    collFreq[i] = calculateCollisionFrequency(self.isomers[i], T, P, self.bathGas)
                
                # Apply method
                if method.lower() == 'modified strong collision':
                    # Modify collision frequencies using efficiency factor
                    for i in range(Nisom): 
                        collFreq[i] *= calculateCollisionEfficiency(self.isomers[i], T, Elist, densStates[i,:], self.collisionModel, E0[i], Ereac[i])
                    # Apply modified strong collision method
                    import msc
                    K[t,p,:,:], p0 = msc.applyModifiedStrongCollisionMethod(T, P, Elist, densStates, collFreq, Kij, Fim, Gnj, Ereac, Nisom, Nreac, Nprod)
                elif method.lower() == 'reservoir state':
                    # The full collision matrix for each isomer
                    Mcoll = numpy.zeros((Nisom,Ngrains,Ngrains), numpy.float64)
                    for i in range(Nisom):
                        Mcoll[i,:,:] = collFreq[i] * self.collisionModel.generateCollisionMatrix(Elist, T, densStates[i,:])
                    # Apply reservoir state method
                    import rs
                    K[t,p,:,:], p0 = rs.applyReservoirStateMethod(T, P, Elist, densStates, Mcoll, Kij, Fim, Gnj, Ereac, Nisom, Nreac, Nprod)
                elif method.lower() == 'chemically-significant eigenvalues':
                    # The full collision matrix for each isomer
                    Mcoll = numpy.zeros((Nisom,Ngrains,Ngrains), numpy.float64)
                    for i in range(Nisom):
                        Mcoll[i,:,:] = collFreq[i] * self.collisionModel.generateCollisionMatrix(Elist, T, densStates[i,:])
                    # Apply chemically-significant eigenvalues method
                    import cse
                    K[t,p,:,:], p0 = cse.applyChemicallySignificantEigenvaluesMethod(T, P, Elist, densStates, Mcoll, Kij, Fim, Gnj, eqRatios, Nisom, Nreac, Nprod)
                else:
                    raise NetworkError('Unknown method "%s".' % method)

                logging.debug(K[t,p,0:Nisom+Nreac+Nprod,0:Nisom+Nreac])

                logging.debug('')

        # Unshift energy grains
        for rxn in self.pathReactions:
            rxn.transitionState.E0 += Emin
        Elist += Emin

        return K

    def drawPotentialEnergySurface(self, fstr):
        """
        Generates an SVG file containing a rendering of the current potential
        energy surface for this reaction network. The SVG file is saved to a
        file at location `fstr` on disk.
        """

        # Determine order of wells based on order of path reactions, but put
        # all the unimolecular isomer wells first
        wells = []
        for isomer in self.isomers: wells.append([isomer])
        for rxn in self.pathReactions:
            if rxn.reactants not in wells:
                if len(rxn.products) == 1 and rxn.products[0] in self.isomers:
                    if self.isomers.index(rxn.products[0]) < len(self.isomers) / 2:
                        wells.insert(0, rxn.reactants)
                    else:
                        wells.append(rxn.reactants)
                else:
                    wells.append(rxn.reactants)
            if rxn.products not in wells:
                if len(rxn.reactants) == 1 and rxn.reactants[0] in self.isomers:
                    if self.isomers.index(rxn.reactants[0]) < len(self.isomers) / 2:
                        wells.insert(0, rxn.products)
                    else:
                        wells.append(rxn.products)
                else:
                    wells.append(rxn.products)
        
        # Drawing parameters
        padding_left = 96.0
        padding_right = padding_left
        padding_top = padding_left / 2.0
        padding_bottom = padding_left / 2.0
        wellWidth = 64.0; wellSpacing = 64.0; Emult = 5.0; TSwidth = 16.0
        E0 = [sum([spec.E0 for spec in well]) / 4184 for well in wells]
        E0.extend([rxn.transitionState.E0 / 4184 for rxn in self.pathReactions])
        y_E0 = (max(E0) - 0.0) * Emult + padding_top
        
        # Determine naive position of each well (one per column)
        coordinates = numpy.zeros((len(wells), 2), numpy.float64)
        x = padding_left + wellWidth / 2.0
        for i, well in enumerate(wells):
            E0 = sum([spec.E0 for spec in well]) / 4184
            y = y_E0 - E0 * Emult
            coordinates[i] = [x, y]
            x += wellWidth + wellSpacing

        # Squish columns together from the left where possible until an isomer is encountered
        Nleft = wells.index([self.isomers[0]])
        for i in range(Nleft-1, -1, -1):
            newX = float(coordinates[i,0])
            for j in range(i+1, Nleft):
                if abs(coordinates[i,1] - coordinates[j,1]) < 96:
                    newX = float(coordinates[j,0]) - (wellWidth + wellSpacing)
                    break
                else:
                    newX = float(coordinates[j,0])
            coordinates[i,0] = newX
        # Squish columns together from the right where possible until an isomer is encountered
        Nright = wells.index([self.isomers[-1]])
        for i in range(Nright+2, len(wells)):
            newX = float(coordinates[i,0])
            for j in range(i-1, Nright, -1):
                if abs(coordinates[i,1] - coordinates[j,1]) < 96:
                    newX = float(coordinates[j,0]) + (wellWidth + wellSpacing)
                    break
                else:
                    newX = float(coordinates[j,0])
            coordinates[i,0] = newX

        coordinates[:,0] -= numpy.min(coordinates[:,0]) - padding_left - wellWidth/2.0

        # Determine required size of diagram
        width = numpy.max(coordinates[:,0]) - numpy.min(coordinates[:,0]) + wellWidth + padding_left + padding_right
        height = numpy.max(coordinates[:,1]) - numpy.min(coordinates[:,1]) + 32.0 + padding_top + padding_bottom

        # Create SVG file for potential energy surface
        f = open(fstr, 'w')
        f.write('<?xml version="1.0" standalone="no"?>\n')
        f.write('<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n')
        f.write('<svg width="%ipx" height="%ipx" viewBox="0 0 %i %i" xmlns="http://www.w3.org/2000/svg" version="1.1">\n' % (width, height, width, height))

        # Draw path reactions
        f.write('\t<g font-family="sans" font-size="8pt">\n')
        for rxn in self.pathReactions:
            reac = wells.index(rxn.reactants)
            prod = wells.index(rxn.products)
            E0_reac = sum([spec.E0 for spec in wells[reac]]) / 4184
            E0_prod = sum([spec.E0 for spec in wells[prod]]) / 4184
            E0_TS = rxn.transitionState.E0 / 4184
            if reac < prod:
                x1, y1 = coordinates[reac,:]
                x2, y2 = coordinates[prod,:]
            else:
                x1, y1 = coordinates[prod,:]
                x2, y2 = coordinates[reac,:]
            x1 += wellSpacing / 2.0; x2 -= wellSpacing / 2.0
            if abs(E0_TS - E0_reac) > 0.1 and abs(E0_TS - E0_prod) > 0.1:
                if len(rxn.reactants) == 2:
                    if reac < prod: x0 = x1 + wellSpacing * 0.5
                    else:           x0 = x2 - wellSpacing * 0.5
                elif len(rxn.products) == 2:
                    if reac < prod: x0 = x2 - wellSpacing * 0.5
                    else:           x0 = x1 + wellSpacing * 0.5
                else:
                    x0 = 0.5 * (x1 + x2)
                y0 = y_E0 - E0_TS * Emult
                width1 = (x0 - x1)
                width2 = (x2 - x0)
                f.write('\t\t<text x="%g" y="%g" fill="gray" style="text-anchor: middle;">%.1f</text>\n' % (x0, y0 - 6, E0_TS * 4.184))
                f.write('\t\t<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="black" stroke-width="2"/>\n' % (x0 - TSwidth/2.0, y0, x0+TSwidth/2.0, y0))
                f.write('\t\t<path d="M %g %g C %g %g %g %g %g %g M %g %g C %g %g %g %g %g %g" stroke="gray" stroke-width="1" fill="none"/>\n' % (x1, y1,   x1 + width1/8.0, y1,   x0 - width1/8.0 - TSwidth/2.0, y0,   x0 - TSwidth/2.0, y0,   x0 + TSwidth/2.0, y0,   x0 + width2/8.0 + TSwidth/2.0, y0,   x2 - width2/8.0, y2,   x2, y2))
            else:
                width = (x2 - x1)
                f.write('\t\t<path d="M %g %g C %g %g %g %g %g %g" stroke="gray" stroke-width="1" fill="none"/>\n' % (x1, y1,   x1 + width/4.0, y1,   x2 - width/4.0, y2,   x2, y2))
        f.write('\t</g>\n')

        # Draw wells (after path reactions so that they are on top)
        f.write('\t<g font-family="sans" font-size="8pt">\n')
        for i, well in enumerate(wells):
            x, y = coordinates[i,:]
            E0 = sum([spec.E0 for spec in well]) / 4184
            if len(well) == 1: 
                text = well[0].label
            elif len(well) == 2:
                text = '<tspan x="%g" dy="0em">%s</tspan><tspan x="%g" dy="1.2em">+ %s</tspan>' % (x, well[0].label, x, well[1].label)
            f.write('\t\t<rect x="%g" y="%g" width="%g" height="%gem" fill="white" fill-opacity="0.75"/>' % (x-wellWidth, y + 4, wellWidth * 2, 1.2*len(well)))
            f.write('\t\t<rect x="%g" y="%g" width="%g" height="%gem" fill="white" fill-opacity="0.75"/>' % (x-wellWidth/2.0, y - 18, wellWidth, 1.1))
            f.write('\t\t<text x="%g" y="%g" fill="gray" style="text-anchor: middle;">%.1f</text>\n' % (x, y - 6, E0 * 4.184))
            f.write('\t\t<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="black" stroke-width="4"/>\n' % (x-wellWidth/2.0, y, x+wellWidth/2.0, y))
            f.write('\t\t<text x="%g" y="%g" fill="black" style="text-anchor: middle;">%s</text>\n' % (x, y + 16, text))
        f.write('\t</g>\n')

        # Finish SVG file
        f.write('</svg>\n')
        f.close()


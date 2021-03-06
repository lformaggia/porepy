import numpy as np
import scipy.sparse as sps
import os
import sys

from porepy.viz.exporter import Exporter
from porepy.fracs import importer

from porepy.params import tensor
from porepy.params.bc import BoundaryCondition
from porepy.params.data import Parameters

from porepy.grids.grid import FaceTag
from porepy.grids import coarsening as co

from porepy.numerics.vem import vem_dual, vem_source
from porepy.numerics.fv.transport import upwind
from porepy.numerics.fv import tpfa, mass_matrix

#------------------------------------------------------------------------------#

def add_data_darcy(gb, domain, tol):
    gb.add_node_props(['param', 'is_tangent'])

    apert = 1e-2

    km =   2.5*1e-11
    kf_t = 5*1e-6
    kf_n = 1e2*km

    for g, d in gb:
        param = Parameters(g)

        rock = g.dim == gb.dim_max()
        kxx = km if rock else kf_t
        d['is_tangential'] = True
        perm = tensor.SecondOrder(g.dim, kxx*np.ones(g.num_cells))
        param.set_tensor("flow", perm)

        param.set_source("flow", np.zeros(g.num_cells))

        param.set_aperture(np.power(apert, gb.dim_max() - g.dim))

        bound_faces = g.get_boundary_faces()
        if bound_faces.size != 0:
            bound_face_centers = g.face_centers[:, bound_faces]

            top = bound_face_centers[1, :] > domain['ymax'] - tol
            bottom = bound_face_centers[1, :] < domain['ymin'] + tol
            left = bound_face_centers[0, :] < domain['xmin'] + tol
            right = bound_face_centers[0, :] > domain['xmax'] - tol
            boundary = np.logical_or(left, right)

            labels = np.array(['neu'] * bound_faces.size)
            labels[boundary] = ['dir']

            bc_val = np.zeros(g.num_faces)
            bc_val[bound_faces[left]] = 30*1e6

            param.set_bc("flow", BoundaryCondition(g, bound_faces, labels))
            param.set_bc_val("flow", bc_val)
        else:
            param.set_bc("flow", BoundaryCondition(
                g, np.empty(0), np.empty(0)))

        d['param'] = param

    # Assign coupling permeability
    gb.add_edge_prop('kn')
    for e, d in gb.edges_props():
        g = gb.sorted_nodes_of_edge(e)[0]
        d['kn'] = kf_n / gb.node_prop(g, 'param').get_aperture()

#------------------------------------------------------------------------------#

def add_data_advection(gb, domain, tol):

    # Porosity
    phi_m = 1e-1
    phi_f = 9*1e-1

    # Density
    rho_w = 1e3 # kg m^{-3}
    rho_s = 2*1e3 # kg m^{-3}

    # heat capacity
    c_w = 4*1e3 # J kg^{-1} K^{-1}
    c_s = 8*1e2 # J kg^{-1} K^{-1}

    c_m = phi_m*rho_w*c_w + (1-phi_m)*rho_s*c_s
    c_f = phi_f*rho_w*c_w + (1-phi_f)*rho_s*c_s

    for g, d in gb:
        param = d['param']

        rock = g.dim == gb.dim_max()
        source = np.zeros(g.num_cells)
        param.set_source("transport", source)

        param.set_porosity(1)
        param.set_discharge(d['discharge'])

        bound_faces = g.get_boundary_faces()
        if bound_faces.size != 0:
            bound_face_centers = g.face_centers[:, bound_faces]

            top = bound_face_centers[1, :] > domain['ymax'] - tol
            bottom = bound_face_centers[1, :] < domain['ymin'] + tol
            left = bound_face_centers[0, :] < domain['xmin'] + tol
            right = bound_face_centers[0, :] > domain['xmax'] - tol
            boundary = np.logical_or(left, right)
            labels = np.array(['neu'] * bound_faces.size)
            labels[boundary] = ['dir']

            bc_val = np.zeros(g.num_faces)
            bc_val[bound_faces[left]] = 1

            param.set_bc("transport", BoundaryCondition(
                g, bound_faces, labels))
            param.set_bc_val("transport", bc_val)
        else:
            param.set_bc("transport", BoundaryCondition(
                g, np.empty(0), np.empty(0)))
        d['param'] = param

    # Assign coupling discharge
    gb.add_edge_prop('param')
    for e, d in gb.edges_props():
        g = gb.sorted_nodes_of_edge(e)[1]
        discharge = gb.node_prop(g, 'param').get_discharge()
        d['param'] = Parameters(g)
        d['param'].set_discharge(discharge)

#------------------------------------------------------------------------------#
#------------------------------------------------------------------------------#

tol = 1e-4
export_folder = 'example_5_2_1'

T = 40*np.pi*1e7
Nt = 20 # 10 20 40 80 160 320 640 1280 2560 5120 - 100000
deltaT = T/Nt
export_every = 1
if_coarse = True

mesh_kwargs = {}
mesh_kwargs['mesh_size'] = {'mode': 'weighted',
                            'value': 500,
                            'bound_value': 500,
                            'tol': tol}

domain = {'xmin': 0, 'xmax': 700, 'ymin': 0, 'ymax': 600}
gb = importer.from_csv('network.csv', mesh_kwargs, domain)
gb.compute_geometry()
if if_coarse:
    co.coarsen(gb, 'by_volume')
gb.assign_node_ordering()

gb.add_node_props(['face_tags'])
for g, d in gb:
    d['face_tags'] = g.face_tags.copy()

internal_flag = FaceTag.FRACTURE
[g.remove_face_tag_if_tag(FaceTag.BOUNDARY, internal_flag) for g, _ in gb]

# Assign parameters
add_data_darcy(gb, domain, tol)

# Choose and define the solvers and coupler
solver_flow = vem_dual.DualVEMMixDim('flow')
A_flow, b_flow = solver_flow.matrix_rhs(gb)

solver_source = vem_source.IntegralMixDim('flow')
A_source, b_source = solver_source.matrix_rhs(gb)

up = sps.linalg.spsolve(A_flow+A_source, b_flow+b_source)
solver_flow.split(gb, "up", up)

gb.add_node_props(["p", "P0u", "discharge"])
solver_flow.extract_u(gb, "up", "discharge")
solver_flow.extract_p(gb, "up", "p")
solver_flow.project_u(gb, "discharge", "P0u")

# compute the flow rate
total_flow_rate = 0
for g, d in gb:
    bound_faces = g.get_boundary_faces()
    if bound_faces.size != 0:
        bound_face_centers = g.face_centers[:, bound_faces]
        left = bound_face_centers[0, :] < domain['xmin'] + tol
        flow_rate = d['discharge'][bound_faces[left]]
        total_flow_rate += np.sum(flow_rate)

save = Exporter(gb, 'darcy', export_folder, binary=False)
save.write_vtk(["p", "P0u"])

#################################################################

for g, d in gb:
    g.face_tags = d['face_tags']

physics = 'transport'
advection = upwind.UpwindMixDim(physics)
mass = mass_matrix.MassMatrixMixDim(physics)
invMass = mass_matrix.InvMassMatrixMixDim(physics)

# Assign parameters
add_data_advection(gb, domain, tol)

gb.add_node_prop('deltaT', prop=deltaT)

U, rhs_u = advection.matrix_rhs(gb)
M, _ = mass.matrix_rhs(gb)
OF = advection.outflow(gb)
M_U = M + U

rhs = rhs_u

# Perform an LU factorization to speedup the solver
IE_solver = sps.linalg.factorized((M_U).tocsc())

theta = np.zeros(rhs.shape[0])

# Loop over the time
time = np.empty(Nt)
file_name = "theta"
i_export = 0
step_to_export = np.empty(0)

production = np.zeros(Nt)
save.change_name("theta")

for i in np.arange(Nt):
    print("Time step", i, " of ", Nt, " time ", i*deltaT, " deltaT ", deltaT)
    # Update the solution
    production[i] = np.sum(OF.dot(theta))/total_flow_rate
    theta = IE_solver(M.dot(theta) + rhs)

    if i%export_every == 0:
        print("Export solution at", i)
        advection.split(gb, "theta", theta)
        save.write_vtk(["theta"], i_export)
        step_to_export = np.r_[step_to_export, i]
        i_export += 1

save.write_pvd(step_to_export*deltaT)

times = deltaT*np.arange(Nt)
np.savetxt(export_folder + '/production.txt', (times, np.abs(production)),
           delimiter=',')

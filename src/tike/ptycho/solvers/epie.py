import logging

import cupy as cp

from tike.linalg import lstsq, projection, norm, orthogonalize_gs
from tike.opt import get_batch, put_batch, randomizer

from ..object import positivity_constraint, smoothness_constraint
from ..probe import orthogonalize_eig

logger = logging.getLogger(__name__)


def epie(
    op, comm,
    data, probe, scan, psi,
    cg_iter=4,
    cost=None,
    eigen_probe=None,
    eigen_weights=None,
    num_batch=1,
    subset_is_random=True,
    probe_options=None,
    position_options=None,
    object_options=None,
    batches=None,
):  # yapf: disable
    """Solve the ptychography problem using extended ptychographical engine.
    """
    for n in randomizer.permutation(num_batch):

        bdata = comm.pool.map(get_batch, data, batches, n=n)
        bscan = comm.pool.map(get_batch, scan, batches, n=n)

        unique_probe = probe
        beigen_probe = None
        beigen_weights = None

        nearplane, cost = zip(*comm.pool.map(
            _update_wavefront,
            bdata,
            unique_probe,
            bscan,
            psi,
            op=op,
        ))

        if comm.use_mpi:
            cost = comm.Allreduce_reduce(cost, 'cpu')
        else:
            cost = comm.reduce(cost, 'cpu')

        (
            psi,
            probe,
            beigen_probe,
            beigen_weights,
        ) = _update_nearplane(
            op,
            comm,
            nearplane,
            psi,
            bscan,
            probe,
            unique_probe,
            beigen_probe,
            beigen_weights,
            object_options is not None,
            probe_options is not None,
        )

        comm.pool.map(
            put_batch,
            bscan,
            scan,
            batches,
            n=n,
        )

    if probe_options and probe_options.orthogonality_constraint:
        probe = comm.pool.map(orthogonalize_eig, probe, xp=cp)

    if object_options:
        psi = comm.pool.map(positivity_constraint,
                            psi,
                            r=object_options.positivity_constraint)

        psi = comm.pool.map(smoothness_constraint,
                            psi,
                            a=object_options.smoothness_constraint)

    result = {
        'psi': psi,
        'probe': probe,
        'cost': cost,
        'scan': scan,
    }
    if position_options:
        result['position_options'] = position_options
    if probe_options:
        result['probe_options'] = probe_options
    if object_options:
        result['object_options'] = object_options

    return result


def _update_wavefront(data, varying_probe, scan, psi, op=None):

    # Compute the diffraction patterns for all of the probe modes at once.
    # We need access to all of the modes of a position to solve the phase
    # problem. The Ptycho operator doesn't do this natively, so it's messy.
    patches = cp.zeros(data.shape, dtype='complex64')
    patches = op.diffraction.patch.fwd(
        patches=patches,
        images=psi,
        positions=scan,
        patch_width=varying_probe.shape[-1],
    )
    patches = patches.reshape(*scan.shape[:-1], 1, 1, op.detector_shape,
                              op.detector_shape)

    nearplane = cp.tile(patches, reps=(1, 1, varying_probe.shape[-3], 1, 1))
    pad, end = op.diffraction.pad, op.diffraction.end
    nearplane[..., pad:end, pad:end] *= varying_probe

    # Solve the farplane phase problem ----------------------------------------
    farplane = op.propagation.fwd(nearplane, overwrite=True)
    intensity = cp.sum(
        cp.square(cp.abs(farplane)),
        axis=list(range(1, farplane.ndim - 2)),
    )
    cost = op.propagation.cost(data, intensity)
    logger.info('%10s cost is %+12.5e', 'farplane', cost)

    farplane *= (cp.sqrt(data) / (cp.sqrt(intensity) + 1e-9))[..., None,
                                                              None, :, :]

    farplane = op.propagation.adj(farplane, overwrite=True)

    return farplane[..., pad:end, pad:end], cost


def max_amplitude(x, **kwargs):
    """Return the maximum of the absolute square."""
    return (x * x.conj()).real.max(**kwargs)


def _update_nearplane(
    op,
    comm,
    nearplane_,
    psi,
    scan_,
    probe,
    unique_probe,
    eigen_probe,
    eigen_weights,
    recover_psi,
    recover_probe,
    step_length=1.0,
):

    patches = comm.pool.map(_get_patches, nearplane_, psi, scan_, op=op)
    step_length /= scan_[0].shape[0]

    for m in range(probe[0].shape[-3]):

        common_grad_psi, common_grad_probe = zip(*comm.pool.map(
            _get_nearplane_gradients,
            nearplane_,
            patches,
            psi,
            scan_,
            probe,
            m=m,
            recover_psi=recover_psi,
            recover_probe=recover_probe,
            op=op,
        ))

        if recover_psi:
            common_grad_psi = comm.reduce(common_grad_psi, 'gpu')[0]
            psi[0] += step_length * common_grad_psi
            psi = comm.pool.bcast([psi[0]])

        if recover_probe:
            common_grad_probe = comm.reduce(common_grad_probe, 'gpu')[0]
            probe[0][..., [m], :, :] += step_length * common_grad_probe
            probe = comm.pool.bcast([probe[0]])

    return psi, probe, eigen_probe, eigen_weights


def _get_patches(nearplane, psi, scan, op=None):
    patches = op.diffraction.patch.fwd(
        patches=cp.zeros(
            nearplane[..., 0, 0, :, :].shape,
            dtype='complex64',
        ),
        images=psi,
        positions=scan,
    )[..., None, None, :, :]
    return patches


def _get_nearplane_gradients(
    nearplane,
    patches,
    psi,
    scan,
    probe,
    m=0,
    recover_psi=True,
    recover_probe=True,
    op=None,
):
    diff = nearplane[..., [m], :, :] - (probe[..., [m], :, :] * patches)

    if recover_psi:
        grad_psi = cp.conj(probe[..., [m], :, :]) * diff / max_amplitude(
            probe[..., [m], :, :],
            keepdims=True,
            axis=(-1, -2),
        )
        common_grad_psi = op.diffraction.patch.adj(
            patches=grad_psi[..., 0, 0, :, :],
            images=cp.zeros(psi.shape, dtype='complex64'),
            positions=scan,
        )

    if recover_probe:
        grad_probe = cp.conj(patches) * diff / max_amplitude(
            patches,
            keepdims=True,
            axis=(-1, -2),
        )
        common_grad_probe = cp.sum(
            grad_probe,
            axis=-5,
            keepdims=True,
        )

    return common_grad_psi, common_grad_probe

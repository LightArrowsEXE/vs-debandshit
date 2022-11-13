from __future__ import annotations

from functools import partial
from math import sqrt

from vsexprtools import aka_expr_available, norm_expr, norm_expr_planes
from vskernels import Bilinear, Point, Scaler, ScalerT
from vsrgtools import box_blur, gauss_blur
from vstools import (
    ColorRange, ColorRangeT, PlanesT, check_ref_clip, check_variable, cround, depth, expect_bits, normalize_planes,
    normalize_seq, vs
)

from .types import GuidedFilterMode

__all__ = [
    'guided_filter'
]


def guided_filter(
    clip: vs.VideoNode, guidance: vs.VideoNode | None = None, radius: int | None = None,
    thr: float = 1 / 3, regulation: list[float] | None = None, mode: GuidedFilterMode = GuidedFilterMode.GRADIENT,
    use_gauss: bool = False, planes: PlanesT = None, range_in: ColorRangeT | None = None,
    down_ratio: int = 0, downscaler: ScalerT = Point, upscaler: ScalerT = Bilinear
) -> vs.VideoNode:
    assert check_variable(clip, guided_filter)

    planes = normalize_planes(clip, planes)

    downscaler = Scaler.ensure_obj(downscaler, guided_filter)
    upscaler = Scaler.ensure_obj(upscaler, guided_filter)

    range_in = ColorRange.from_param(range_in, guided_filter) or ColorRange.from_video(clip)

    width, height = clip.width, clip.height

    if regulation is None:
        thr = normalize_seq(thr, clip.format.num_planes)

        size = normalize_seq([220, 225, 225] if range_in.is_full else 256, clip.format.num_planes)

        regulation = [t / s for t, s in zip(thr, size)]

    if radius is None:
        width_c = width / (1 << clip.format.subsampling_w)
        height_c = height / (1 << clip.format.subsampling_h)

        rad = max((width - 1280) / 160 + 12, (height - 720) / 90 + 12)
        radc = max((width_c - 1280) / 160 + 12, (height_c-720) / 90 + 12)
        radius = [round(rad), round(radc)][:clip.format.num_planes]

    check_ref_clip(clip, guidance)

    p, bits = expect_bits(clip, 32)
    guidance_clip = g = depth(guidance, 32) if guidance is not None else p

    r = normalize_seq(radius, clip.format.num_planes)
    eps = normalize_seq(regulation, clip.format.num_planes)

    if down_ratio:
        down_w = cround(width / down_ratio)
        down_h = cround(height / down_ratio)

        p = downscaler.scale(p, down_w, down_h)
        g = downscaler.scale(g, down_w, down_h) if guidance is not None else p

        r = cround(r / down_ratio)

    blur_filter = partial(
        gauss_blur, sigma=[val / 2 * sqrt(2) for val in r], planes=planes
    ) if use_gauss else partial(
        box_blur, radius=[val + 1 for val in r], planes=planes
    )

    blur_filter_corr = partial(
        gauss_blur, sigma=1/2 * sqrt(2), planes=planes
    ) if use_gauss else partial(box_blur, radius=2, planes=planes)

    mean_p = blur_filter(p)
    mean_I = blur_filter(g) if guidance is not None else mean_p

    I_square = norm_expr(g, 'x dup *', planes)
    corr_I = blur_filter(I_square)
    corr_Ip = blur_filter(norm_expr([g, p], 'x y *', planes)) if guidance is not None else corr_I

    var_I = norm_expr([corr_I, mean_I], 'x y dup * -', planes)
    cov_Ip = norm_expr([corr_Ip, mean_I, mean_p], 'x y z * -', planes) if guidance is not None else var_I

    if mode is GuidedFilterMode.ORIGINAL:
        a = norm_expr([cov_Ip, var_I], 'x y {eps} + /', planes, eps=eps)
    else:
        if r == 1:
            var_I_1 = var_I
        else:
            mean_I_1 = blur_filter_corr(g)
            corr_I_1 = blur_filter_corr(I_square)
            var_I_1 = norm_expr([corr_I_1, mean_I_1], 'x y dup * -', planes)

        if mode is GuidedFilterMode.WEIGHTED:
            weight_in = var_I_1
        else:
            weight_in = norm_expr([var_I, var_I_1], 'x y * sqrt', planes)

        denominator = norm_expr([weight_in], '1 x {eps} + /', planes, eps=1e-06)

        denominator = denominator.std.PlaneStats(None, 0)

        if aka_expr_available:
            weight = norm_expr([weight_in, denominator], 'x 1e-06 + y.PlaneStatsAverage *', planes)
        else:
            weight = denominator.std.FrameEval(
                lambda n, f: weight_in.std.Expr(
                    norm_expr_planes(weight_in, f'x 1e-06 + {f.props.PlaneStatsAverage} *', planes)
                ), denominator
            )

        if mode is GuidedFilterMode.WEIGHTED:
            a = norm_expr([cov_Ip, var_I, weight], 'x y {eps} z / + /', planes, eps=eps)
        else:
            weight_in = weight_in.std.PlaneStats(None, 0)

            if aka_expr_available:
                a = norm_expr(
                    [cov_Ip, weight_in, weight, var_I],
                    'x {eps} 1 1 1 -4 y.PlaneStatsMin y.PlaneStatsAverage 1e-6 - - / '
                    'y y.PlaneStatsAverage - * exp + / - * z / + a {eps} z / + /',
                    planes, eps=eps
                )
            else:
                def _gradient(n, f):
                    frameMean = f.props.PlaneStatsAverage

                    return norm_expr(
                        [cov_Ip, weight_in, weight, var_I],
                        'x {eps} 1 1 1 {kk} y {alpha} - * exp + / - * z / + a {eps} z / + /',
                        planes, eps=eps, kk=-4 / (f.props.PlaneStatsMin - frameMean - 1e-6), alpha=frameMean
                    )

                a = weight.std.FrameEval(_gradient, weight_in)

    b = norm_expr([mean_p, a, mean_I], 'x y z * -', planes)

    mean_a, mean_b = blur_filter(a), blur_filter(b)

    if down_ratio:
        mean_a = upscaler.scale(mean_a, width, height)
        mean_b = upscaler.scale(mean_b, width, height)

    q = norm_expr([mean_a, guidance_clip, mean_b], 'x y * z +', planes)

    return depth(q, bits)

/**
 * Scroll mapping for the Documents editor line-number gutter.
 *
 * The textarea wraps lines but the gutter does not, so their scroll heights
 * differ. Mapping the scroll ratio (not scrollTop) keeps them aligned at both
 * ends; with no wrapping it is the identity.
 */
export function gutterScrollTop(taScrollTop, taScrollHeight, taClientHeight, gScrollHeight, gClientHeight) {
  const taMax = taScrollHeight - taClientHeight;
  const gMax = gScrollHeight - gClientHeight;
  if (taMax <= 0 || gMax <= 0) return 0;
  const ratio = Math.min(1, Math.max(0, taScrollTop / taMax));
  return ratio * gMax;
}

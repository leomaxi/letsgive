// Icon + wordmark lockup, reused everywhere the app names itself (the
// dashboard header, the auth pages) so a future rebrand or logo refresh
// only needs to change one place. The heart badge is a fixed brand color
// in both themes (not `dark:`-adapted) -- it's the literal logo, not
// theme-sensitive UI chrome.
export default function BrandMark({
  size = "md",
  className = "",
}: {
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const iconSize = size === "lg" ? 32 : size === "sm" ? 20 : 24;
  const textClass = size === "lg" ? "text-2xl" : size === "sm" ? "text-base" : "text-lg";

  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <svg width={iconSize} height={iconSize} viewBox="0 0 24 24" aria-hidden="true" className="flex-shrink-0">
        <rect width="24" height="24" rx="6" fill="#254dd1" />
        <path
          fill="#ffffff"
          d="M12,18.6l-1.02-0.93C7.14,14.24,4.5,11.86,4.5,8.94
             C4.5,6.56,6.36,4.7,8.74,4.7c1.32,0,2.59,0.62,3.26,1.55
             c0.67-0.93,1.94-1.55,3.26-1.55c2.38,0,4.24,1.86,4.24,4.24
             c0,2.92-2.64,5.3-6.48,8.74L12,18.6z"
        />
      </svg>
      <span className={`whitespace-nowrap font-semibold text-brand-700 dark:text-brand-400 ${textClass}`}>
        Let's Give
      </span>
    </span>
  );
}

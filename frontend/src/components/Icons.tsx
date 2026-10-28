type P = { size?: number; strokeWidth?: number; color?: string }

const base = (size: number, strokeWidth: number, color = 'currentColor') => ({
  width: size,
  height: size,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: color,
  strokeWidth,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
})

export const LogoMark = ({ size = 18, strokeWidth = 2.2 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <polyline points="3 17 9 11 13 15 21 7" />
    <polyline points="15 7 21 7 21 13" />
  </svg>
)

export const Bell = ({ size = 20, strokeWidth = 1.8 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
    <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
  </svg>
)

export const UserIcon = ({ size = 20, strokeWidth = 1.8 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <circle cx="12" cy="8" r="4" />
    <path d="M4 21a8 8 0 0 1 16 0" />
  </svg>
)

export const Power = ({ size = 18, strokeWidth = 2.4 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <path d="M12 2v10" />
    <path d="M18.4 6.6a9 9 0 1 1-12.77.04" />
  </svg>
)

export const Plus = ({ size = 16, strokeWidth = 2.2 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <path d="M12 5v14M5 12h14" />
  </svg>
)

export const Check = ({ size = 14, strokeWidth = 3, color }: P) => (
  <svg {...base(size, strokeWidth, color)}>
    <polyline points="4 12 10 18 20 6" />
  </svg>
)

export const Home = ({ size = 22, strokeWidth = 1.9 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <path d="M3 11l9-7 9 7" />
    <path d="M5 10v10h14V10" />
  </svg>
)

export const Trend = ({ size = 22, strokeWidth = 1.9 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <polyline points="3 17 9 11 13 15 21 7" />
  </svg>
)

export const List = ({ size = 22, strokeWidth = 1.9 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <path d="M8 6h13M8 12h13M8 18h13" />
    <path d="M3 6h.01M3 12h.01M3 18h.01" />
  </svg>
)

export const Gear = ({ size = 22, strokeWidth = 1.9 }: P) => (
  <svg {...base(size, strokeWidth)}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
  </svg>
)


    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            xdark: 'rgb(from var(--xsa-bg) r g b / <alpha-value>)',
            xcard: 'rgb(from var(--xsa-panel) r g b / <alpha-value>)',
            xcardhover: 'rgb(from var(--xsa-panel-raised) r g b / <alpha-value>)',
            xborder: 'rgb(from var(--xsa-line) r g b / <alpha-value>)',
            xblue: 'rgb(from var(--xsa-cyan) r g b / <alpha-value>)',
            xpurple: 'rgb(from var(--xsa-accent) r g b / <alpha-value>)',
            xgreen: 'rgb(from var(--xsa-success) r g b / <alpha-value>)',
            xred: 'rgb(from var(--xsa-danger) r g b / <alpha-value>)',
            xamber: 'rgb(from var(--xsa-warning) r g b / <alpha-value>)',
          },
          fontFamily: { mono: ['JetBrains Mono', 'Consolas', 'monospace'] }
        }
      }
    }
  
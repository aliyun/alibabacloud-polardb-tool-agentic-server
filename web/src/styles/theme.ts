import type { ThemeConfig } from 'antd'

export const appTheme: ThemeConfig = {
  token: {
    // Brand colors
    colorPrimary: '#0071e3',
    colorSuccess: '#34c759',
    colorWarning: '#ff9f0a',
    colorError: '#ff3b30',
    colorInfo: '#5ac8fa',

    // Typography
    fontFamily:
      'Inter, -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    fontSize: 14,

    // Border radius
    borderRadius: 10,
    borderRadiusLG: 14,
    borderRadiusSM: 6,

    // Layout colors
    colorBgLayout: '#f5f5f7',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',

    // Borders
    colorBorder: 'rgba(60, 60, 67, 0.10)',
    colorBorderSecondary: 'rgba(60, 60, 67, 0.06)',

    // Shadows
    boxShadow: '0 1px 3px rgba(0,0,0,0.06), 0 0 0 0.5px rgba(0,0,0,0.04)',
    boxShadowSecondary: '0 4px 12px rgba(0,0,0,0.08)',

    // Motion
    motionDurationMid: '0.25s',
    motionEaseInOut: 'cubic-bezier(0.25, 0.1, 0.25, 1)',
  },
  components: {
    Button: {
      borderRadius: 8,
      controlHeight: 36,
      fontWeight: 500,
    },
    Card: {
      borderRadiusLG: 14,
      paddingLG: 20,
    },
    Table: {
      borderRadius: 10,
      headerBg: '#fafafc',
      headerColor: '#6e6e73',
      headerBorderRadius: 10,
      fontSize: 13,
    },
    Input: {
      borderRadius: 8,
      controlHeight: 38,
    },
    Select: {
      borderRadius: 8,
      controlHeight: 38,
    },
    Menu: {
      itemBorderRadius: 8,
      itemMarginInline: 8,
    },
    Tag: {
      borderRadiusSM: 6,
    },
    Modal: {
      borderRadiusLG: 16,
    },
    Message: {
      borderRadiusLG: 10,
    },
  },
}

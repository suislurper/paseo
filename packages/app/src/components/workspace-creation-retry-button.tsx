import { useCallback } from "react";
import { Pressable, Text, type PressableStateCallbackType } from "react-native";
import { StyleSheet } from "react-native-unistyles";
import type { WorkspaceCreationAttempt } from "@/screens/workspace-creation-attempt";

export function WorkspaceCreationRetryButton(input: {
  attempt: WorkspaceCreationAttempt | null;
  errorMessage: string | null;
  created: boolean;
  disabled: boolean;
  onPress: () => void;
  label: string;
  testID: string;
}) {
  const disabled = input.disabled;
  const buttonStyle = useCallback(
    ({ pressed }: PressableStateCallbackType) => [
      styles.button,
      disabled || pressed ? styles.dimmed : null,
    ],
    [disabled],
  );
  if (!input.attempt || !input.errorMessage || input.created) return null;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={input.label}
      disabled={disabled}
      onPress={input.onPress}
      style={buttonStyle}
      testID={input.testID}
    >
      <Text style={styles.label}>{input.label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create((theme) => ({
  button: {
    marginTop: theme.spacing[2],
    paddingHorizontal: theme.spacing[4],
    paddingVertical: theme.spacing[2],
    borderRadius: theme.borderRadius["2xl"],
    backgroundColor: theme.colors.surface1,
    alignSelf: "flex-start",
  },
  dimmed: { opacity: 0.6 },
  label: { fontSize: theme.fontSize.sm, color: theme.colors.foreground },
}));

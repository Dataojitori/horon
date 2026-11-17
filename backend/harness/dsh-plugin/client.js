// Browser half of the Horon dsh plugin: the settings page behind the Configure button of the
// `horon` row on the Plugins page. Plain JS, no build step. The form components come from the
// page's own ui-primitives; nothing is installed for it. Structure follows the official
// @deepseek-ai/dsh-client-ui-settings-web-search page.
window.__ModuleLoader__.load({
  id: 'horon',
  factory(require) {
    const React = require('react');
    const primitives = require('@deepseek-ai/dsh-client-ui-primitives');
    const store = require('@deepseek-ai/dsh-client-store');
    const h = React.createElement;

    const NS = 'settings.horon';
    const ROW_KEY = 'horon#horon'; // <package name>#<row id>
    const CONFIG_NS = 'horon'; // the plugin's `name`, which names its settings section

    const OPENROUTER = { field: 'openrouterKey', refField: 'openrouterKeyEnv', fallback: 'HORON_OPENROUTER_API_KEY' };
    const REMOTE = { field: 'remoteKey', refField: 'remoteKeyEnv', fallback: 'HORON_REMOTE_KEY' };
    const SECRETS = [OPENROUTER, REMOTE];

    const en = {
      title: 'Horon',
      summary: 'Memory data directory, API keys and the memory reminder.',
      dataDir: 'Data directory',
      dataDirHint: 'Where Horon keeps its data. Leave blank to use the default location.',
      openrouterKey: 'OpenRouter API key',
      openrouterKeyHint: 'Stored outside the settings file. Leave blank to keep the current key.',
      openrouterKeySet: 'A key is configured.',
      openrouterKeyUnset: 'No key is configured; semantic search is unavailable.',
      remoteKey: 'AISoulKeeper cloud key',
      remoteKeyHint: 'Stored outside the settings file. Leave blank to keep the current key.',
      remoteKeySet: 'A key is configured; cloud memory is used.',
      remoteKeyUnset: 'No key is configured; the local database is used.',
      removeRemoteKey: 'Remove the cloud key and use the local database',
      removeFailed: 'The key was not removed: ',
      startupReminder: 'Remind the AI to read its memory in a new session and after compaction',
      subagents: 'Subagents also use Horon (opening reminder, hooks and the horon tool)',
      overridden: 'Overridden',
      reset: 'Reset to default',
      readOnly: 'This deployment stores settings read-only.',
      unavailable: 'This plugin is not loaded, so it cannot be configured right now.',
      save: 'Save',
      saving: 'Saving…',
      saveFailed: 'The deployment did not accept these values; they were left for you to correct.',
    };
    const zh = {
      title: 'Horon',
      summary: '记忆数据目录、API 密钥和读取记忆提醒。',
      dataDir: '数据目录',
      dataDirHint: 'Horon 存放数据的位置。留空使用默认位置。',
      openrouterKey: 'OpenRouter API key',
      openrouterKeyHint: '不写入设置文件。留空表示保持当前密钥。',
      openrouterKeySet: '已配置密钥。',
      openrouterKeyUnset: '未配置密钥；语义搜索不可用。',
      remoteKey: 'AISoulKeeper 云端 key',
      remoteKeyHint: '不写入设置文件。留空表示保持当前密钥。',
      remoteKeySet: '已配置密钥；使用云端记忆。',
      remoteKeyUnset: '未配置密钥；使用本地库。',
      removeRemoteKey: '移除云端 key，改用本地库',
      removeFailed: '没有移除成功：',
      startupReminder: '新会话和压缩后提醒 AI 读取记忆',
      subagents: '子代理也接入 Horon（开局提醒、钩子和 horon 工具）',
      overridden: '已覆盖',
      reset: '恢复默认',
      readOnly: '本部署的设置为只读。',
      unavailable: '该插件当前未加载，暂时无法配置。',
      save: '保存',
      saving: '保存中…',
      saveFailed: '本部署没有接受这些值，已保留供你修改。',
    };

    function formLabels(t) {
      return {
        unavailable: t('unavailable'),
        readOnly: t('readOnly'),
        saveFailed: t('saveFailed'),
        save: t('save'),
        saving: t('saving'),
      };
    }

    // A boolean as a staged text field: 'true' / 'false'. `fallback` is the default.
    function booleanField(field, fallback) {
      return {
        field,
        format: (v) => ((typeof v === 'boolean' ? v : fallback) ? 'true' : 'false'),
        parse: (text) => ({ kind: 'set', value: text === 'true' }),
      };
    }

    function refOf(snapshot, secret) {
      const declared = snapshot.value && snapshot.value[secret.refField];
      return typeof declared === 'string' && declared.length > 0 ? declared : secret.fallback;
    }

    // The form model over the `horon` section, plus the state of the two credentials.
    class HoronCardController {
      constructor(scope, ctx) {
        this.scope = scope;
        this.ctx = ctx;
        this.disposed = false;
        this.credentials = {};
        for (const s of SECRETS) this.credentials[s.field] = { ref: '', configured: false, writable: true };
        this.form = new primitives.SettingsFormModel(
          scope,
          [primitives.settingsTextField('dataDir'), booleanField('startupReminder', true), booleanField('subagents', false)],
          SECRETS.map((s) => ({ field: s.field, write: (text) => this.writeKey(s, text) })),
        );
        this.store = store.createSnapshotStore(this.projection());
        const publish = () => {
          this.store.set(this.projection());
        };
        this.unsubscribe = [
          this.form.bind(() => this.form.shell()).subscribe(publish),
          scope.subscribe(() => {
            this.readCredentials();
          }),
        ];
        this.readCredentials();
      }

      projection() {
        const shell = this.form.shell();
        const snapshot = this.scope.getSnapshot();
        const out = {
          ...shell,
          dataDir: this.form.field('dataDir'),
          startupReminder: this.form.field('startupReminder').text !== 'false',
          startupReminderOverridden: this.form.field('startupReminder').overridden,
          subagents: this.form.field('subagents').text === 'true',
        };
        for (const s of SECRETS) {
          const cred = this.credentials[s.field];
          out[s.field] = this.form.field(s.field);
          out[s.field + 'Configured'] = cred.configured;
          out[s.field + 'Writable'] = cred.writable;
        }
        out.removeError = this.removeError || '';
        void snapshot;
        return out;
      }

      async readCredentials() {
        if (this.disposed) return;
        const snapshot = this.scope.getSnapshot();
        const refs = SECRETS.map((s) => refOf(snapshot, s));
        const response = await this.ctx.remote.credentials.describe(refs);
        if (this.disposed || !response.ok) return;
        SECRETS.forEach((s, i) => {
          const view = response.value[refs[i]];
          this.credentials[s.field] = {
            ref: refs[i],
            configured: (view && view.configured) || false,
            writable: view ? view.writable !== false : true,
          };
        });
        this.store.set(this.projection());
      }

      refreshCredential(ref) {
        if (SECRETS.some((s) => this.credentials[s.field].ref === ref)) this.readCredentials();
      }

      async writeKey(secret, value) {
        const ref = refOf(this.scope.getSnapshot(), secret);
        await this.ctx.remote.credentials.set(ref, value);
        await this.readCredentials();
        return this.credentials[secret.field].configured;
      }

      // Leaving the field blank keeps the key, so removing it is a separate action.
      async removeKey(secret) {
        const ref = refOf(this.scope.getSnapshot(), secret);
        const response = await this.ctx.remote.credentials.unset(ref);
        this.removeError = response.ok ? '' : response.error.message;
        await this.readCredentials();
        this.store.set(this.projection());
      }

      inject() {
        return {
          hooks: { horonCard: this.store },
          removeRemoteKey: () => {
            this.removeKey(REMOTE);
          },
          edit: (field, text) => {
            this.form.actions().edit(field, text);
          },
          resetField: (field) => {
            this.form.actions().resetField(field);
          },
          save: () => {
            this.form.actions().save();
          },
          discard: () => {
            this.form.actions().discard();
          },
        };
      }

      dispose() {
        this.disposed = true;
        for (const off of this.unsubscribe) off();
        this.form.dispose();
      }
    }

    function HoronCard(props) {
      const t = props.t;
      if (props.view === 'summary') return t('summary');
      const state = props.useHoronCard((snapshot) => snapshot);
      const disabled = !state.writable || state.saving;
      const secretField = (secret) =>
        h(primitives.SettingsSecretField, {
          key: secret.field,
          id: 'plugin-config-horon-' + secret.field,
          label: t(secret.field),
          hint: t(secret.field + 'Hint'),
          disabled: !state[secret.field + 'Writable'],
          text: state[secret.field].text,
          configured: state[secret.field + 'Configured'],
          stateLabel: t(secret.field + (state[secret.field + 'Configured'] ? 'Set' : 'Unset')),
          onEdit: (text) => {
            props.edit(secret.field, text);
          },
        });
      return h(
        primitives.SettingsForm,
        { labels: formLabels(t), state, onSave: props.save, onDiscard: props.discard },
        h(primitives.SettingsValueField, {
          id: 'plugin-config-horon-data-dir',
          label: t('dataDir'),
          hint: t('dataDirHint'),
          overriddenLabel: t('overridden'),
          resetLabel: t('reset'),
          invalidLabel: '',
          disabled,
          ...state.dataDir,
          onEdit: (text) => {
            props.edit('dataDir', text);
          },
          onReset: () => {
            props.resetField('dataDir');
          },
        }),
        secretField(OPENROUTER),
        secretField(REMOTE),
        state.remoteKeyConfigured && state.remoteKeyWritable
          ? h(
              'div',
              { style: { marginTop: -4, marginBottom: 12, fontSize: 13 } },
              h(
                'button',
                {
                  type: 'button',
                  disabled: state.saving,
                  onClick: props.removeRemoteKey,
                  style: {
                    padding: '4px 10px',
                    borderRadius: 6,
                    border: '1px solid var(--dsw-alias-separator-primary, #ccc)',
                    background: 'transparent',
                    color: 'var(--dsw-alias-label-primary)',
                    cursor: 'pointer',
                  },
                },
                t('removeRemoteKey'),
              ),
              state.removeError ? h('span', { style: { marginLeft: 8 } }, t('removeFailed') + state.removeError) : null,
            )
          : null,
        h(
          'label',
          {
            style: {
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'flex-start',
              gap: 16,
              fontSize: 13,
              lineHeight: 1.5,
              color: 'var(--dsw-alias-label-primary)',
            },
          },
          h('span', null, t('startupReminder')),
          h(primitives.Switch, {
            checked: state.startupReminder,
            label: t('startupReminder'),
            disabled,
            onChange: (checked) => {
              props.edit('startupReminder', checked ? 'true' : 'false');
            },
          }),
        ),
        h(
          'label',
          {
            style: {
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'flex-start',
              marginTop: 12,
              gap: 16,
              fontSize: 13,
              lineHeight: 1.5,
              color: 'var(--dsw-alias-label-primary)',
            },
          },
          h('span', null, t('subagents')),
          h(primitives.Switch, {
            checked: state.subagents,
            label: t('subagents'),
            disabled,
            onChange: (checked) => {
              props.edit('subagents', checked ? 'true' : 'false');
            },
          }),
        ),
      );
    }

    return {
      inject: ['slots', 'locale', 'remote', 'remote.credentials', 'configForms'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'horon: dictionaries');
        const card = new HoronCardController(ctx.configForms.get(CONFIG_NS), ctx);
        ctx.effect(() => () => {
          card.dispose();
        }, 'horon: form subscription');
        ctx.effect(
          () =>
            ctx.remote.$on('credentials/reference-updated', (ref) => {
              card.refreshCredential(ref);
            }),
          'horon: credential invalidations',
        );
        ctx.effect(
          () =>
            ctx.configForms.whileServed([CONFIG_NS], () =>
              ctx.slots.inject('plugins.row.config', () =>
                ctx.slots.register(
                  { name: 'plugins.row.config', key: ROW_KEY, locale: NS, inject: () => card.inject() },
                  HoronCard,
                ),
              ),
            ),
          'horon: page',
        );
      },
    };
  },
});
